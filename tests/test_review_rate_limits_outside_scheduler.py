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

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, Prepared, PublishResult
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.services.publishing.sync import GitSync


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


class OrganizationLookupRateLimited:
    """A DEV API whose organization lookup is rate-limited (``Retry-After: 900``) until ``limited`` is cleared; the
    article POST succeeds. Counts the sends."""

    def __init__(self):
        self.limited, self.sends = True, 0
        self.platform = DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(self)))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/organizations/acme":
            if self.limited:
                return httpx2.Response(429, headers={"Retry-After": "900"}, json={"error": "slow down"})
            return httpx2.Response(200, json={"id": 7, "name": "Acme", "username": "acme"})
        if request.method == "POST" and request.url.path == "/api/articles":
            self.sends += 1
            return httpx2.Response(201, json={"id": 1000 + self.sends, "url": f"https://dev.to/acme/{self.sends}",
                                              "published": True})
        return httpx2.Response(404, json={"error": "not found"})


class GitSyncNowPostToAnOrganization(unittest.TestCase):
    """Item 2: a Git sync "now" post the platform rate-limits waits for the scheduler's retry. ``publish()`` resolves a
    destination before the post row exists ("so a typo fails now"), and a 429 there still ends the post 'failed' at
    once: no row, no retry, no Retry-After, and the next CI run creates it afresh, paced by nothing (the item's own
    case, for any post with an organization or publication)."""

    def test_a_429_on_the_destination_lookup_is_retried_by_the_scheduler(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        user = store.upsert_user("local:on-call", "on-call", None, None)["id"]
        dev = OrganizationLookupRateLimited()
        store.save_credentials(user, "devto", {"api_key": "k"}, {"id": "1", "name": "on-call"})
        publisher = Publisher(store, {"devto": dev.platform})
        sync = GitSync(store, {"devto": dev.platform}, publisher)
        req = SyncRequest(source="github.com/acme/blog", files=[SyncFile(path="posts/now.md", markdown=(
            "---\ntitle: Now\nplatforms: [devto]\ndevto:\n  organization: acme\n---\n\nBody.\n"))])
        (first,) = asyncio.run(sync.sync(user, req)).results
        self.assertEqual((first.action, first.status, dev.sends), ("create", "scheduled", 0))
        post = store.get_post(first.post_id)
        self.assertAlmostEqual(post["run_at"] - post["updated_at"], 900, delta=2, msg="the 429's Retry-After")
        (again,) = asyncio.run(sync.sync(user, req)).results   # the next CI run, during the wait
        self.assertEqual((again.action, again.post_id, dev.sends), ("unchanged", first.post_id, 0))
        dev.limited = False
        asyncio.run(publisher.run_due(now=time.time() + 901))
        self.assertEqual((store.get_post(first.post_id)["status"], dev.sends), ("published", 1))


if __name__ == "__main__":
    unittest.main()
