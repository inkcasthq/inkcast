# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #473 (the OB3 review of PR #456): a note written while Git sync's update of a published post is out is kept.

``Publisher.update`` reads the post's notes, words the error it keeps from that read (#392, #234), and wrote it with
no fence. A note landing between the read and the write was overwritten, so the person was never told: another worker's
late success naming another copy (``PostsRepo.record_success``'s "may have this post twice", #214), or an earlier
send's uncertain answer landing over a re-send (``_lost``, #234). #456's review fenced ``settle_edited`` the same way
(``update_post(if_error=)``); now ``update`` is fenced on the error it read, and on a miss words it again from the row.
Both ``Finding`` classes failed on main."""

import unittest
from unittest import mock

from inkcast.libs.pipeline import _lost

from test_review_sync_427_456 import TWICE
from test_sync_edit_during_send_392 import Editable
from test_sync_edited_note_427 import EditedWhileOut
from test_sync_error_after_post import SyncCase


def lands_before_the_updates_write(case, land):
    """``store.update_post`` that runs ``land(post_id)`` once, just before ``Publisher.update``'s write (the one call
    that writes a payload): another worker's write, through the same store, between the update's read and its write."""
    real, landed = case.store.update_post, []

    def update_post(post_id, *args, **fields):
        if not landed and "payload" in fields:
            landed.append(land(post_id))
        return real(post_id, *args, **fields)

    return update_post, landed


class FindingASecondCopyNoteWrittenMeanwhileOutlivesTheUpdate(EditedWhileOut):
    def test_a_note_written_between_the_updates_read_and_its_write_is_kept(self):
        when, post = self.success_lands_after(lambda when, post: self.sync_async(when, "Edited."))
        self.assertIsNotNone(self.note(post["id"]))

        def other_worker(post_id):
            row = self.store.get_post(post_id)
            return self.store.record_success(
                post_id, held=row["attempts"], status="published", result_url="https://editable/9", remote_id="9",
                sent={}, notes=[], second_copy=str, both_copies=lambda _row: TWICE, edited="")

        patched, landed = lands_before_the_updates_write(self, other_worker)
        with mock.patch.object(self.store, "update_post", patched):
            synced = self.sync(when, body="Edited.")
        self.assertEqual((synced.action, [written for written, _ in landed]), ("update", [False]),
                         "the other worker's answer did not land as a second copy")
        after = self.store.get_post(post["id"])
        self.assertIn(TWICE, self.store.notes_of(after), "the update overwrote a second-copy note written after its read")
        self.assertIsNone(self.note(post["id"]), "the update did not settle the edit it sent")
        self.assertEqual((self.body_of(after), self.platform.updated), ("Edited.", [("1", "Edited.")]))


class FindingALostAnswerNoteWrittenOverANoteFreePostOutlivesTheUpdate(SyncCase):
    platform_class = Editable

    def test_a_note_written_over_a_post_with_no_notes_is_kept(self):
        """The fence on a row whose error is NULL: ``if_error=None`` is no fence at all, so the update fences on ""."""
        first = self.sync()
        self.assertEqual(first.status, "published")
        self.store.update_post(first.post_id, attempts=2)                     # published by a re-send, attempt 2
        self.assertIsNone(self.store.get_post(first.post_id)["error"])
        lost = _lost(self.platform)

        def attempt_1s_late_answer(post_id):
            """``Publisher._uncertain``'s write of an uncertain answer to attempt 1, landing over the re-send's copy (#559)."""
            return self.store.record_uncertain(post_id, held=1, error="Editable timed out.", lost=lost)[1] is not None

        patched, landed = lands_before_the_updates_write(self, attempt_1s_late_answer)
        with mock.patch.object(self.store, "update_post", patched):
            synced = self.sync(body="Edited.")
        self.assertEqual((synced.action, landed), ("update", [True]), "the late answer did not land")
        after = self.store.get_post(first.post_id)
        self.assertEqual((after["status"], self.platform.updated), ("published", [("1", "Edited.")]))
        self.assertIn(lost, self.store.notes_of(after), "the update overwrote the second-copy flag written after its read")


if __name__ == "__main__":
    unittest.main()
