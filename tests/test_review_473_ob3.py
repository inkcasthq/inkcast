# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The OB3 review of the change for issue #473: ``Publisher.update``'s fence on the notes it read.

``update`` words the post's error from a read of its notes and writes it only while the notes are as read
(``update_post(if_error=)``); on a miss it words them again from the row as it is now. Rewording keeps the other notes
only when the row holds the edited-while-out note (or keeps ``_lost`` alone), so a second-copy note that lands on a post
with no such note made the write miss and was then dropped by the second wording: the fence fired and the note was lost
anyway (``FindingASecondCopyNoteOverANoteFreePostOutlivesTheUpdate``, failed on c0d45d98). The ``Held`` classes pin
what the fence must keep: it ends on a row whose error is stored as "" and on one whose note keeps changing for a while,
and an update with no note written meanwhile writes what it wrote before."""

import unittest
from unittest import mock

from test_review_sync_427_456 import TWICE
from test_sync_edit_during_send_392 import Editable
from test_sync_edited_note_427 import EditedWhileOut
from test_sync_error_after_post import SyncCase
from test_sync_update_fence_473 import lands_before_the_updates_write


def another_workers_late_success(case):
    """``PostsRepo.record_success`` of another worker's send that made another copy (id 9): it notes both copies."""

    def land(post_id):
        row = case.store.get_post(post_id)
        return case.store.record_success(
            post_id, held=row["attempts"], status="published", result_url="https://editable/9", remote_id="9",
            sent={}, notes=[], second_copy=str, both_copies=lambda _row: TWICE, edited="")

    return land


class FindingASecondCopyNoteOverANoteFreePostOutlivesTheUpdate(SyncCase):
    platform_class = Editable

    def test_a_second_copy_note_written_over_a_post_with_no_notes_is_kept(self):
        first = self.sync()
        self.assertEqual((first.status, self.store.get_post(first.post_id)["error"]), ("published", None))

        patched, landed = lands_before_the_updates_write(self, another_workers_late_success(self))
        with mock.patch.object(self.store, "update_post", patched):
            synced = self.sync(body="Edited.")
        self.assertEqual((synced.action, [written for written, _ in landed]), ("update", [False]),
                         "the other worker's answer did not land as a second copy")
        after = self.store.get_post(first.post_id)
        self.assertEqual((after["status"], self.platform.updated), ("published", [("1", "Edited.")]))
        self.assertIn(TWICE, self.store.notes_of(after), "the update overwrote a second-copy note written after its read")


class HeldTheFenceEndsOnAnEmptyStringError(SyncCase):
    platform_class = Editable

    def test_an_error_stored_as_empty_text_is_read_as_no_notes(self):
        """``row["error"] or ""`` and ``COALESCE(error, '')`` read "" and NULL alike: one write, no second loop."""
        first = self.sync()
        self.store.update_post(first.post_id, error="")
        calls = []
        real = self.store.update_post

        def counted(post_id, *args, **fields):
            if "payload" in fields:
                calls.append(fields.get("if_error"))
            return real(post_id, *args, **fields)

        with mock.patch.object(self.store, "update_post", counted):
            self.assertEqual(self.sync(body="Edited.").action, "update")
        self.assertEqual(calls, [""])
        self.assertIsNone(self.store.get_post(first.post_id)["error"])


class HeldTheFenceEndsOnceTheNotesStopChanging(SyncCase):
    platform_class = Editable

    def test_three_notes_written_meanwhile_take_four_writes(self):
        first = self.sync()
        calls = []
        real = self.store.update_post

        def busy(post_id, *args, **fields):
            if "payload" in fields:
                calls.append(fields.get("if_error"))
                if len(calls) <= 3:  # another worker writes a note before each of the first three writes
                    real(post_id, error=f"note {len(calls)}")
            return real(post_id, *args, **fields)

        with mock.patch.object(self.store, "update_post", busy):
            self.assertEqual(self.sync(body="Edited.").action, "update")
        self.assertEqual(calls, ["", "note 1", "note 2", "note 3"])
        self.assertEqual(self.store.get_post(first.post_id)["status"], "published")


class HeldAnUpdateWithNothingWrittenMeanwhileWritesAsBefore(SyncCase):
    platform_class = Editable

    def test_platform_notes_of_the_earlier_send_are_replaced(self):
        """No race: the update's own wording, as before #473: a note neither edited nor lost is the update's to replace."""
        first = self.sync()
        self.store.update_post(first.post_id, error="Editable trimmed the tags.")
        self.assertEqual(self.sync(body="Edited.").action, "update")
        after = self.store.get_post(first.post_id)
        self.assertEqual((after["status"], after["error"], self.platform.updated), ("published", None, [("1", "Edited.")]))


class HeldANormalFlowLeavesNoNote(EditedWhileOut):
    def test_send_follow_edit_update_revert(self):
        """The note an edit while out leaves (#392) is settled by the update that sends the edit, and a revert after it
        is another update, never a note that stays (``settle_edited``'s ``holds`` keeps nothing in this flow)."""
        when, post = self.success_lands_after(lambda when, post: self.sync_async(when, "Edited."))
        self.assertIsNotNone(self.note(post["id"]))
        self.assertEqual(self.sync(when, body="Edited.").action, "update")
        self.assertEqual(self.sync(when, body="Body.").action, "update")
        self.assertEqual(self.sync(when, body="Body.").action, "unchanged")
        self.assertEqual((self.store.get_post(post["id"])["error"], self.platform.updated),
                         (None, [("1", "Edited."), ("1", "Body.")]))


if __name__ == "__main__":
    unittest.main()
