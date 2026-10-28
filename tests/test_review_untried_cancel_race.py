# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #276 (#204, ADR-0017): a post the scheduler tries while a sync replaces it is never replaced.

The sync reads an untried scheduled post (attempts 0) and takes the cancel-and-replace path. Its cancel only asks that
the post be pending, so if the scheduler claims and sends it between the sync's read and its cancel, the sync cancels a
post the scheduler has tried:
- rate-limited (a 429): a post waiting for its retry, which ADR-0017 says is changed in place, never cancelled and
  replaced. The replacement is due at the file's time, which has passed, and goes out at the next tick, before the
  platform's Retry-After, with its attempts back at 0 (#204's symptom).
- answered uncertainly (a timeout after sending, a 5xx): 'unconfirmed', which may already be on the platform. The
  replacement is a second copy that no person decided on (#226).

The scheduler runs in another process (here a thread with its own connection), between the sync's read and its cancel:
the same interleave as the PR's own claim-race test, one step further on."""

import asyncio
import threading
import time
import unittest
from unittest import mock

from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import PlatformError

from test_sync_error_after_post import Findable, SyncCase, at

WAIT = 300  # the 429's Retry-After


class AnUntriedPostTriedDuringTheSync(SyncCase):
    def raced(self, error: PlatformError):
        """A body edit of a due, untried post; the scheduler of another process claims and sends it first, and the
        platform answers ``error``. The sync's result, the post, and what the other scheduler sent."""
        created = self.sync(at(-600))
        post = self.store.get_post(created.post_id)
        self.assertEqual((post["status"], post["attempts"]), ("scheduled", 0))
        path = self.store._conn.execute("PRAGMA database_list").fetchone()["file"]
        elsewhere = Findable()
        elsewhere.error = error
        real = self.publisher.cancel

        def tried_first(*args, **kwargs):
            other = Store(path, "k" * 40)
            try:
                tick = threading.Thread(target=lambda: asyncio.run(Publisher(other, {elsewhere.id: elsewhere}).run_due()))
                tick.start()
                tick.join()
            finally:
                other.close()
            return real(*args, **kwargs)

        with mock.patch.object(self.publisher, "cancel", tried_first):
            result = self.sync(at(-600), body="Edited.")
        self.assertEqual(elsewhere.sent, ["Body."], "the other scheduler never sent the post")
        return result, post

    def test_a_post_rate_limited_meanwhile_keeps_its_wait(self):
        result, post = self.raced(PlatformError("Findable rate limit hit.", retryable=True, retry_after=WAIT))
        self.assertEqual(result.post_id, post["id"], "the post waiting for its retry was cancelled and replaced")
        self.tick = lambda when: asyncio.run(self.publisher.run_due(now=when))
        self.tick(time.time() + 1)
        self.assertEqual(self.platform.sent, [], "sent again before the platform's Retry-After")
        self.assertEqual(self.sync(at(-600), body="Edited.").action, "update")
        waiting = self.store.get_post(post["id"])
        self.assertEqual((waiting["status"], waiting["attempts"]), ("scheduled", 1))
        self.assertAlmostEqual(waiting["run_at"] - time.time(), WAIT, delta=5)
        self.assertEqual(len(self.posts()), 1)
        self.tick(waiting["run_at"] + 1)
        self.assertEqual(self.platform.sent, ["Edited."])

    def test_a_post_unconfirmed_meanwhile_is_never_sent_again(self):
        result, post = self.raced(PlatformError("No answer from Findable.", uncertain=True))
        asyncio.run(self.publisher.run_due(now=time.time() + 1))
        self.assertEqual(self.platform.sent, [], "a second copy of a post that may already be on the platform")
        self.assertEqual([p["status"] for p in self.posts()], ["unconfirmed"], "the person's decision was taken for them")
        self.assertEqual(self.sync(at(-600), body="Edited.").status, "unconfirmed")
        self.assertEqual(len(self.posts()), 1)


if __name__ == "__main__":
    unittest.main()
