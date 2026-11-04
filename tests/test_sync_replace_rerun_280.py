# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #280: a replacement remade after a failed run keeps the replaced post's ``created_at``.

On the replace path (an edited scheduled post) the sync cancels the post and claims the file in one transaction, then
makes the replacement with the cancelled post's ``created_at`` when only the content changed (ADR-0015): how late it is
counts from when its time was first asked for, and the scheduler claims due posts in the order
``MAX(run_at, created_at)``. When the run failed before the replacement's row existed, the claim named nothing, and the
next sync made the replacement as a new post, created now: a past-due replacement was then claimed after a post that was
due later. Now the claim names the post it replaces, and the re-run keeps that post's creation as the replace run does."""

import time
import unittest

from test_sync_error_after_post import Flaky, SyncCase, at


class AReplaceThatFailedBeforeItsRow(SyncCase):
    def scheduled(self) -> dict:
        (live,) = [p for p in self.posts() if p["status"] == "scheduled"]
        return live

    def claimed_before_a_post_due_later(self, post: dict) -> bool:
        """``post``, due five minutes ago, against one that was due (and made) two minutes later: the scheduler's order."""
        now = time.time()
        self.store._exec("UPDATE posts SET run_at = ? WHERE id = ?", now - 300, post["id"])
        later = self.store.create_post(group_id="grp_later", user_id=self.user, platform=self.platform.id, title="Later",
                                       status="scheduled", run_at=now - 200, payload={}, created_at=now - 200)
        order = [p["id"] for p in self.store.claim_due_posts(now)]
        self.assertEqual(sorted(order), sorted([post["id"], later]))
        return order[0] == post["id"]

    def test_the_rerun_keeps_the_replaced_posts_creation(self):
        when = at(2 * 24 * 3600)
        first = self.sync(when)
        born = time.time() - 600
        self.store._exec("UPDATE posts SET created_at = ? WHERE id = ?", born, first.post_id)  # synced ten minutes ago
        # The replace run: the cancel and the claim are written; the replacement's row is the write that is locked.
        self.sync_locked(Flaky(self.store.create_post, lambda *a, **k: True), "create_post", when, body="Better body.")
        self.assertEqual(self.store.get_post(first.post_id)["status"], "cancelled")
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("failed", None))

        rerun = self.sync(when, body="Better body.")
        live = self.scheduled()
        self.assertEqual((rerun.status, rerun.post_id), ("scheduled", live["id"]))
        self.assertEqual(live["created_at"], born, "the replacement remade by the re-run was created now, not when "
                         "the replaced post was")
        self.assertEqual(self.sync(when, body="Better body.").action, "unchanged")
        self.assertTrue(self.claimed_before_a_post_due_later(live), "claimed after a post that was due later")

    def test_the_replace_run_itself_keeps_it_as_before(self):
        """What the fix keeps: the run that does the replacement keeps the creation when only the content changed, and
        takes a new one when the schedule changed too."""
        when = at(2 * 24 * 3600)
        first = self.sync(when)
        born = time.time() - 600
        self.store._exec("UPDATE posts SET created_at = ? WHERE id = ?", born, first.post_id)
        replaced = self.sync(when, body="Better body.")
        self.assertEqual((replaced.action, self.scheduled()["created_at"]), ("update", born))
        moved = self.sync(at(3 * 24 * 3600), body="Better body.")
        self.assertEqual(moved.action, "update")
        self.assertGreater(self.scheduled()["created_at"], born + 500)

    def test_a_rerun_whose_file_moved_the_schedule_takes_a_new_creation(self):
        """The re-run applies the replace run's rule to the file as it is now: a schedule that differs from the replaced
        post's is a new creation, as it would have been had the replace run succeeded."""
        when = at(2 * 24 * 3600)
        first = self.sync(when)
        born = time.time() - 600
        self.store._exec("UPDATE posts SET created_at = ? WHERE id = ?", born, first.post_id)
        self.sync_locked(Flaky(self.store.create_post, lambda *a, **k: True), "create_post", when, body="Better body.")
        rerun = self.sync(at(3 * 24 * 3600), body="Better body.")
        self.assertEqual(rerun.status, "scheduled")
        self.assertGreater(self.scheduled()["created_at"], born + 500)


if __name__ == "__main__":
    unittest.main()
