# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #204: an author's change to a post waiting for its retry is made in place, keeping the platform's wait.

A post the scheduler tried, and that a 429 sent back to wait (``run_at`` is the retry's), was replaced by a body edit:
a new post, untried, due at the file's time, which had passed. It went out at the next tick, before the Retry-After,
with its attempts back at 0. A time moved ahead but before the retry did the same, and a removed time read as
unchanged. Now the post is changed in place (ADR-0017): same row, same attempts, ``run_at`` the later of the time asked
for and the platform's wait (``not_before``), and ``requested_at`` holds the time asked for, which the sync compares
with."""

import asyncio
import contextlib
import sqlite3
import time
import unittest
from typing import Iterator
from unittest import mock

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.pipeline import Outcome
from inkcast.libs.platforms import PlatformError

from test_sync_error_after_post import PATH, SOURCE, SyncCase, at, markdown

WAIT = 300  # the 429's Retry-After: within the default policy's delays, so the retry is this far away


class Waiting(SyncCase):
    def waiting(self, publish_at=None) -> dict:
        """A post whose time has passed, whose send the platform rate-limited: it waits WAIT s for its retry."""
        created = self.sync(at(-600) if publish_at is None else publish_at)
        self.platform.error = PlatformError("Findable rate limit hit.", retryable=True, retry_after=WAIT)
        asyncio.run(self.publisher.run_due())
        self.platform.error = None
        post = self.store.get_post(created.post_id)
        self.assertEqual((post["status"], post["attempts"], self.platform.sent), ("scheduled", 1, ["Body."]))
        self.assertAlmostEqual(post["run_at"] - time.time(), WAIT, delta=5)
        self.assertEqual(post["not_before"], post["run_at"])
        return post

    def tick(self, at_time: float) -> None:
        asyncio.run(self.publisher.run_due(now=at_time))

    def assert_waits(self, post: dict, until: float) -> dict:
        """The one post is the waiting one, still waiting, with its attempt, until ``until``."""
        posts = self.posts()
        self.assertEqual([p["id"] for p in posts], [post["id"]], "replaced by a new post")
        now = posts[0]
        self.assertEqual((now["status"], now["attempts"]), ("scheduled", 1))
        self.assertAlmostEqual(now["run_at"], until, delta=1)
        return now


class FindingABodyEditKeepsTheWait(Waiting):
    def test_a_body_edit_during_the_wait_goes_out_at_the_retry_with_the_new_body(self):
        post = self.waiting()
        before = self.entry()
        edited = self.sync(at(-600), body="Edited.")
        self.assertEqual((edited.action, edited.status, edited.post_id), ("update", "scheduled", post["id"]))
        self.assertIn("waits for the retry", edited.message)
        self.assert_waits(post, post["run_at"])
        self.assertNotEqual(self.entry()["content_hash"], before["content_hash"], "the entry kept the old content")
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("scheduled", post["id"]))

        self.tick(post["run_at"] - 5)
        self.assertEqual(self.platform.sent, ["Body."], "sent before the platform's wait was over")
        self.assertEqual(self.sync(at(-600), body="Edited.").action, "unchanged")
        self.tick(post["run_at"] + 1)
        self.assertEqual(self.platform.sent, ["Body.", "Edited."])
        self.assertEqual((self.store.get_post(post["id"])["status"], self.store.get_post(post["id"])["attempts"]),
                         ("published", 2))
        self.assertEqual(self.sync(at(-600), body="Edited.").action, "unchanged")

    def test_an_undated_post_the_platform_rate_limited_keeps_its_wait_too(self):
        """A "now" post (no publish_at): the sync's own send got the 429 and handed it to the scheduler (#188)."""
        self.platform.error = PlatformError("Findable rate limit hit.", retryable=True, retry_after=WAIT)
        created = self.sync(None)
        self.platform.error = None
        post = self.store.get_post(created.post_id)
        self.assertEqual((post["status"], post["attempts"], post["requested_at"]), ("scheduled", 1, None))
        self.assertEqual(self.sync(None).action, "unchanged")
        self.assertEqual(self.sync(None, body="Edited.").action, "update")
        self.assert_waits(post, post["run_at"])
        self.tick(post["run_at"] - 5)
        self.assertEqual(self.platform.sent, ["Body."])


class FindingATimeChangeKeepsTheWait(Waiting):
    def test_a_time_moved_ahead_but_before_the_retry_waits_for_the_retry(self):
        post = self.waiting()
        moved = at(WAIT - 100)
        self.assertEqual(self.sync(moved).action, "update")
        now = self.assert_waits(post, post["run_at"])
        self.assertEqual(now["requested_at"], moved.timestamp())
        self.tick(moved.timestamp() + 1)
        self.assertEqual(self.platform.sent, ["Body."], "sent at the file's time, before the platform's wait was over")
        self.assertEqual(self.sync(moved).action, "unchanged", "the same file reads as changed at every sync")
        self.tick(post["run_at"] + 1)
        self.assertEqual(self.platform.sent, ["Body.", "Body."])

    def test_a_time_moved_past_the_retry_and_back_keeps_the_wait(self):
        post = self.waiting()
        later = at(WAIT + 600)
        self.sync(later)
        self.assert_waits(post, later.timestamp())
        self.tick(post["run_at"] + 1)
        self.assertEqual(self.platform.sent, ["Body."], "sent before the time written")
        back = at(WAIT - 100)
        self.sync(back)
        self.assert_waits(post, post["run_at"])

    def test_a_removed_time_is_recorded_and_still_waits(self):
        """Concern 3 of #203: a removed time read as unchanged. It is a change now, and "now" is after the wait."""
        post = self.waiting()
        removed = self.sync(None)
        self.assertEqual((removed.action, removed.post_id), ("update", post["id"]))
        self.assertIsNone(self.assert_waits(post, post["run_at"])["requested_at"])
        self.assertEqual(self.sync(None).action, "unchanged")
        self.tick(post["run_at"] - 5)
        self.assertEqual(self.platform.sent, ["Body."])

    def test_a_time_moved_into_the_past_still_waits(self):
        post = self.waiting()
        self.assertEqual(self.sync(at(-60)).action, "update")
        self.assert_waits(post, post["run_at"])
        self.tick(post["run_at"] - 5)
        self.assertEqual(self.platform.sent, ["Body."])

    def test_an_unchanged_file_is_left_alone(self):
        post = self.waiting()
        self.assertEqual(self.sync(at(-600)).action, "unchanged")
        self.assertEqual(self.store.get_post(post["id"])["updated_at"], post["updated_at"])


@contextlib.contextmanager
def refused_ledger_writes(store) -> Iterator[None]:
    """Every write of a ledger entry fails while this is open."""
    store._exec("CREATE TEMP TRIGGER refuse_entry BEFORE INSERT ON ledger BEGIN SELECT RAISE(ABORT, 'database is locked'); END")
    try:
        yield
    finally:
        store._exec("DROP TRIGGER temp.refuse_entry")


class FindingAnEditThatCannotLandChangesNothing(Waiting):
    def test_a_failed_entry_write_rolls_the_edit_back(self):
        post = self.waiting()
        with refused_ledger_writes(self.store), self.assertRaises(sqlite3.DatabaseError):
            self.sync(at(-600), body="Edited.")
        self.assertEqual(self.store.get_post(post["id"])["payload"], post["payload"], "the post changed, its entry didn't")
        self.assertEqual(self.sync(at(-600), body="Edited.").action, "update")

    def test_a_post_the_scheduler_claimed_meanwhile_is_left_to_it(self):
        post = self.waiting()
        real = self.publisher.refusal

        async def claimed_first(*args, **kwargs):  # the scheduler's tick lands between the sync's read and its write
            self.store.claim_due_posts(post["run_at"] + 1)
            return await real(*args, **kwargs)

        with mock.patch.object(self.publisher, "refusal", claimed_first):
            raced = self.sync(at(-600), body="Edited.")
        self.assertEqual((raced.action, raced.post_id), ("skipped", post["id"]))
        now = self.store.get_post(post["id"])
        self.assertEqual((now["status"], now["attempts"], now["payload"]), ("publishing", 2, post["payload"]))

    def test_a_refused_change_leaves_the_post_and_its_entry(self):
        """A destination the platform doesn't know (a typo in the edit) fails now, as for a new post; the waiting post
        keeps what it had, and the next sync tries the change again."""
        post = self.waiting()
        entry = self.entry()

        async def typo(user_id, story, platform, prepared, retry=False):
            return Outcome(platform.id, "failed", message="Findable has no publication 'typo'.")

        with mock.patch.object(self.publisher, "refusal", typo):
            refused = self.sync(at(-600), body="Edited.")
        self.assertEqual((refused.action, refused.status, refused.post_id), ("failed", "scheduled", post["id"]))
        self.assertEqual(self.store.get_post(post["id"]), post)
        self.assertEqual((self.entry()["content_hash"], self.entry()["status"]), (entry["content_hash"], "scheduled"))
        self.assertEqual(self.sync(at(-600), body="Edited.").action, "update")

    def test_a_dry_run_changes_nothing(self):
        post = self.waiting()
        req = SyncRequest(source=SOURCE, dry_run=True, files=[
            SyncFile(path=PATH, markdown=markdown(self.platform.id, at(-600), "Edited."))])
        (dry,) = asyncio.run(self.git.sync(self.user, req)).results
        self.assertEqual((dry.action, dry.post_id), ("update", post["id"]))
        self.assertEqual(self.store.get_post(post["id"]), post)


class FindingARowFromBeforeTheColumns(Waiting):
    def test_a_waiting_post_from_before_the_columns_keeps_its_retry(self):
        """No requested_at and no not_before: its run_at is the retry's. The file reads as changed once (its time is
        compared with nothing), and the edit in place keeps the retry."""
        post = self.waiting()
        self.store._exec("UPDATE posts SET requested_at = NULL, not_before = NULL WHERE id = ?", post["id"])
        self.assertEqual(self.sync(at(-600)).action, "update")
        self.assert_waits(post, post["run_at"])
        self.assertEqual(self.sync(at(-600)).action, "unchanged")
        self.tick(post["run_at"] - 5)
        self.assertEqual(self.platform.sent, ["Body."])


class FindingWhatIsRecorded(Waiting):
    def test_a_post_records_the_time_it_was_asked_for(self):
        when = at(2 * 24 * 3600)
        scheduled = self.store.get_post(self.sync(when).post_id)
        self.assertEqual((scheduled["requested_at"], scheduled["not_before"]), (when.timestamp(), None))

    def test_recovery_sending_again_records_no_wait_ahead(self):
        """A send interrupted mid-way that the platform says it never got is queued again at once: its not_before is
        that moment, so an edit after it waits for nothing."""
        post = self.waiting()
        self.store._exec("UPDATE posts SET status = 'publishing', attempts = 2 WHERE id = ?", post["id"])
        self.platform.sent.clear()  # the platform has nothing: recovery's lookup answers "missing"
        self.recover()
        again = self.store.get_post(post["id"])
        self.assertIsNotNone(again["not_before"])
        self.assertLess(again["not_before"], post["not_before"] - WAIT / 2, "recovery kept the 429's wait")


if __name__ == "__main__":
    unittest.main()
