# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #280 (PR #414): the lifecycle of ``ledger.replaces``, the post a replace's claim cancelled, kept until the
entry has a post of its own. Regression cover for what the review measured: these pass on the PR head."""

import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

from inkcast.libs.db import Store
from inkcast.libs.db.ledger import CLAIM_TTL
from test_sync_error_after_post import Flaky, SyncCase, at


class ReplacesLifecycle(SyncCase):
    def failed_replace(self, when, born: float) -> str:
        """A scheduled post made ten minutes ago, then a replace whose replacement row is the write that is locked."""
        first = self.sync(when)
        self.store._exec("UPDATE posts SET created_at = ? WHERE id = ?", born, first.post_id)
        self.sync_locked(Flaky(self.store.create_post, lambda *a, **k: True), "create_post", when, body="Better body.")
        return first.post_id

    def live(self) -> dict:
        (post,) = [p for p in self.posts() if p["status"] == "scheduled"]
        return post

    def test_a_second_failure_keeps_it_and_the_run_that_works_clears_it(self):
        when, born = at(2 * 86400), time.time() - 600
        replaced = self.failed_replace(when, born)
        self.sync_locked(Flaky(self.store.create_post, lambda *a, **k: True), "create_post", when, body="Better body.")
        self.assertEqual((self.entry()["status"], self.entry()["post_id"], self.entry()["replaces"]), ("failed", None, replaced))
        self.assertEqual(self.sync(when, body="Better body.").status, "scheduled")
        self.assertEqual((self.live()["created_at"], self.entry()["replaces"]), (born, None))

    def test_a_claim_left_by_a_stopped_process_is_taken_over_with_it(self):
        when, born = at(2 * 86400), time.time() - 600
        first = self.sync(when)
        self.store._exec("UPDATE posts SET created_at = ? WHERE id = ?", born, first.post_id)

        class Stopped(BaseException):
            pass
        with mock.patch.object(self.git, "_create", side_effect=Stopped()), self.assertRaises(Stopped):
            self.sync(when, body="Better body.")                                # stopped after the cancel and claim
        self.assertEqual((self.entry()["status"], self.entry()["replaces"]), ("syncing", first.post_id))
        self.assertEqual(self.sync(when, body="Better body.").action, "skipped")   # the claim is still held
        self.store._exec("UPDATE ledger SET synced_at = ?", time.time() - CLAIM_TTL - 1)
        self.assertEqual(self.sync(when, body="Better body.").status, "scheduled")
        self.assertEqual(self.live()["created_at"], born)

    def test_a_locked_final_write_points_the_entry_at_its_post_and_clears_it(self):
        when, born = at(2 * 86400), time.time() - 600
        first = self.sync(when)
        self.store._exec("UPDATE posts SET created_at = ? WHERE id = ?", born, first.post_id)
        final = Flaky(self.store.put_entry, lambda *a, **k: k.get("status") == "scheduled" and "content_hash" in k)
        self.sync_locked(final, "put_entry", when, body="Better body.")
        self.assertEqual((self.entry()["post_id"], self.entry()["replaces"]), (self.live()["id"], None))
        self.assertEqual(self.live()["created_at"], born)

    def test_a_replaced_post_that_is_gone_gives_a_new_creation(self):
        when, born = at(2 * 86400), time.time() - 600
        replaced = self.failed_replace(when, born)
        self.store._exec("DELETE FROM posts WHERE id = ?", replaced)
        self.assertEqual(self.sync(when, body="Better body.").status, "scheduled")
        self.assertGreater(self.live()["created_at"], born + 500)

    def test_the_schedule_comparison_is_the_replace_runs_own(self):
        """The replaced post's time as written is the same schedule, and any move the file can express is a change
        (``same_time`` in ``_one``; a tolerance of a second let a time moved later by under that go out early, #222)."""
        for moved, kept in ((0.0, True), (0.5, False), (-0.000001, False), (1.0, False)):
            with self.subTest(moved=moved):
                self.setUp()
                when, born = at(2 * 86400), time.time() - 600
                self.failed_replace(when, born)
                self.sync(when + timedelta(seconds=moved), body="Better body.")
                self.assertEqual(self.live()["created_at"] == born, kept)


class ADatabaseFromBeforeTheColumn(unittest.TestCase):
    def test_the_column_is_added_on_open(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = directory + "/inkcast.db"
        Store(path, "k" * 40).close()
        with sqlite3.connect(path) as c:
            c.execute("ALTER TABLE ledger DROP COLUMN replaces")
        store = Store(path, "k" * 40)
        self.addCleanup(store.close)
        self.assertIn("replaces", {r["name"] for r in store._all("PRAGMA table_info(ledger)")})


if __name__ == "__main__":
    unittest.main()
