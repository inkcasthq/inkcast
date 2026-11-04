# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #442 (issue #234, the follow-ups of a person deciding on an unconfirmed post).

Each ``Finding`` class failed on the PR's head (47a531e) and passes with the fix committed beside it. Concurrency is
driven by hooks and asyncio events inside the platform's calls, never by sleeps, as ``test_person_decides`` does: another
worker is a second ``Publisher`` on its own connection, run as a task while the first worker's request is held."""

import asyncio
import time

from inkcast.libs.pipeline import STUCK_AFTER, _lost
from inkcast.libs.platforms import PlatformError, PublishResult

from test_person_decides import Findable, Harness


class Lagging(Findable):
    """Findable whose request reaches the platform before its answer is held: ``during_publish(copy)`` runs once the
    copy exists. A lookup lists only the copies in ``listed`` (DEV's listing can lag a post; the code allows for it,
    "a lookup that lags the post"), and names the newest, as DEV names the one article it can tell is this post."""

    def __init__(self):
        super().__init__()
        self.listed: list[int] = []

    async def publish(self, creds, account, prepared, destination):
        self.sent.append(prepared.title)
        copy = len(self.sent)
        if self.during_publish:
            hook, self.during_publish = self.during_publish, None
            await hook(copy)
        if self.answer is not None:
            answer, self.answer = self.answer, None
            raise answer
        return PublishResult(url=f"https://findable/{copy}", remote_id=str(copy), status="published")

    async def find_existing(self, creds, account, prepared):
        self.lookups += 1
        newest = self.listed[-1] if self.listed else None
        answer = None if newest is None else PublishResult(url=f"https://findable/{newest}", remote_id=str(newest),
                                                           status="published")
        if self.meanwhile:
            hook, self.meanwhile = self.meanwhile, None
            await hook(prepared)
        return answer


# ---- the note that an earlier send may have arrived too (#234 item 3) outlives every later write --------------------


class FindingRecoveryFoundOverUnconfirmedKeepsTheLostNote(Harness):
    platform_class = Lagging

    def test_a_found_answer_landing_after_an_earlier_sends_lost_answer_keeps_the_second_copy_flag(self):
        """A's send (attempt 1) is out; its copy reached the platform but is not listed yet. B's stuck pass is told
        "missing", re-queues the post and re-sends it (attempt 2); copy 2 is listed, B's answer is slow. C's stuck pass
        takes the post over at attempt 2 and finds copy 2. While C's answer is out, A's answer lands: a timeout after
        the request was sent, written over C's 'recovering' as 'unconfirmed' with the note that it may have arrived too
        (``update_post(beyond=)``). C's found answer is now written over 'unconfirmed' (#234 item 1) with error=None:
        the note is gone, B's success (the same copy C found) has nothing to keep, and a post the platform holds twice
        reads as any published post."""
        post_id = self.post("scheduled")
        b, c = self.replica(), self.replica()
        c_asking, release_c, release_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tasks = {}

        async def c_asks(prepared):                                           # inside C's lookup, which found copy 2
            c_asking.set()
            await release_c.wait()

        async def b_sends(copy):                                              # inside B's publish: copy 2 is listed
            self.platform.listed.append(copy)
            self.platform.meanwhile = c_asks
            tasks["c"] = asyncio.create_task(c.run_due(now=time.time() + 2 * (STUCK_AFTER + 1)))
            await release_b.wait()

        async def a_sends(copy):                                              # inside A's publish: copy 1 not listed
            self.platform.during_publish = b_sends
            tasks["b"] = asyncio.create_task(b.run_due(now=time.time() + STUCK_AFTER + 1))
            await c_asking.wait()
            raise PlatformError("Findable timed out after the request was sent.", uncertain=True)

        async def run():
            await self.publisher.run_due(now=time.time())                     # A's tick
            between = dict(self.store.get_post(post_id))
            release_c.set()
            await tasks["c"]
            after_c = dict(self.store.get_post(post_id))
            release_b.set()
            await tasks["b"]
            return between, after_c

        self.platform.during_publish = a_sends
        between, after_c = asyncio.run(run())
        lost = _lost(self.platform)
        self.assertEqual((between["status"], between["attempts"]), ("unconfirmed", 2))
        self.assertIn(lost, between["error"], "A's answer over C's pass did not say it may have arrived too")
        self.assertEqual(after_c["status"], "published")
        self.assertIn(lost, after_c["error"] or "", "C's found answer erased the second-copy flag")
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], self.platform.sent),
                         ("published", "https://findable/2", ["t", "t"]))
        self.assertIn(lost, post["error"] or "", "two copies on the platform, and the post reads as any published post")
