# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #199 (issue #188): rate limits outside the scheduler loop (recovery, Git sync "now" posts,
the messages).

Each test failed on the PR's head before the fix committed with it."""

import asyncio
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, Prepared, PublishResult


class Findable(Platform):
    """A platform that can be checked (``find``): it finds what it published. ``meanwhile`` (an async hook) runs once
    inside a lookup, after the platform has answered and before the caller sees the answer."""

    id, name, default_api_url, credential_fields = "findable", "Findable", "https://findable.invalid", []
    capabilities = frozenset({"find"})

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sent: list[str] = []
        self.meanwhile = None

    async def verify(self, creds):
        return Account(id="1", name="findable")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sent.append(prepared.title)
        return PublishResult(url=f"https://findable/{len(self.sent)}", remote_id=str(len(self.sent)), status="published")

    async def find_existing(self, creds, account, prepared):
        answer = (PublishResult(url="https://findable/1", remote_id="1", status="published")
                  if prepared.title in self.sent else None)
        if self.meanwhile:
            hook, self.meanwhile = self.meanwhile, None
            await hook()
        return answer


class TwoWorkersRecoverOnePost(unittest.TestCase):
    """``claim_stuck`` takes a 'recovering' post again once it has been recovering for STUCK_AFTER ("recovering only
    asks the platform, it never sends"), and a pass recovers its claimed posts one lookup at a time. The PR makes
    'recovering' a state a post comes back to every STUCK_AFTER for as long as the platform refuses the lookup, so a
    long pass (many deferred posts, each lookup up to the 30 s timeout) is overtaken by another replica's. Recovery's
    writes are unconditional: worker A's "missing" answer, read before worker B re-queued and sent the post, puts the
    sent post back in the queue, and it goes out twice. (The race is older than the PR; the PR makes it recur.)"""

    def test_a_stale_missing_answer_does_not_put_a_sent_post_back_in_the_queue(self):
        path = tempfile.mkdtemp() + "/inkcast.db"
        a_store, b_store = Store(path, "k" * 40), Store(path, "k" * 40)   # two scheduler replicas, one database
        for store in (a_store, b_store):
            self.addCleanup(store.close)
        user = a_store.upsert_user("local:on-call", "on-call", None, None)["id"]
        platform = Findable()
        a_store.save_credentials(user, platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
        a, b = Publisher(a_store, {platform.id: platform}), Publisher(b_store, {platform.id: platform})
        prepared = platform.prepare({"title": "t"}, "Body.")
        post = a_store.create_post(group_id="g", user_id=user, platform=platform.id, title="t", status="publishing",
                                   run_at=time.time() - 1, payload=prepared.to_dict())
        a_store._exec("UPDATE posts SET updated_at = updated_at - ?", STUCK_AFTER + 1)   # interrupted mid-send

        async def b_takes_over():
            # A's pass has outlasted STUCK_AFTER: B's tick takes the post over, finds it missing, re-queues and sends it.
            await b.run_due(now=time.time() + STUCK_AFTER + 1)
            self.assertEqual((b_store.get_post(post)["status"], platform.sent), ("published", ["t"]))

        platform.meanwhile = b_takes_over
        with self.assertLogs("inkcast.libs.pipeline", "WARNING"):
            asyncio.run(a.run_due())   # A asked first ("missing"), and writes its answer after B's send
        asyncio.run(a.run_due(now=time.time() + 1))   # the next tick
        self.assertEqual(platform.sent, ["t"], "the post was sent twice")
        self.assertEqual(a_store.get_post(post)["status"], "published")


if __name__ == "__main__":
    unittest.main()
