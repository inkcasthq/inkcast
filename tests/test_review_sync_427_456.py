# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #456 (issue #427): ``Publisher.settle_edited`` clears the "changed while this send was out" note once
the file is back to what was sent, and keeps every other note.

It reads the row, words the kept notes from that read, and writes them back. Its fence is the row's status and attempt
count, but a late success that names another copy (``PostsRepo.record_success``'s "may have this post twice" note, #214)
writes only the error: same status, same attempts. Landing between the read and the write (another worker's send, in
another process), that note was overwritten, and the person was never told the platform may have the post twice."""

import unittest
from unittest import mock

from test_sync_edited_note_427 import EditedWhileOut

TWICE = ("Editable may have this post twice: https://editable/1 (id 1), recorded here, and https://editable/9 (id 9), "
         "attempt 3's answer. Check Editable: keep one and remove the other.")


class FindingASecondCopyNoteWrittenMeanwhileOutlivesTheSettle(EditedWhileOut):
    def test_a_note_written_between_the_settles_read_and_its_write_is_kept(self):
        when, post = self.success_lands_after(lambda when, post: self.sync_async(when, "Edited."))
        self.assertIsNotNone(self.note(post["id"]))
        real, landed = self.store.update_post, []

        def other_worker_lands_first(post_id, *args, **fields):
            """Another worker's success, naming another copy, recorded through the same chokepoint just before the
            settle's write (one-shot)."""
            if not landed and "if_attempts" in fields:
                row = self.store.get_post(post_id)
                landed.append(self.store.record_success(
                    post_id, held=row["attempts"], status="published", result_url="https://editable/9", remote_id="9",
                    sent={}, notes=[], second_copy=str, both_copies=lambda _row: TWICE, edited=""))
            return real(post_id, *args, **fields)

        with mock.patch.object(self.store, "update_post", other_worker_lands_first):
            reverted = self.sync(when, body="Body.")
        self.assertEqual((reverted.action, [written for written, _ in landed]), ("unchanged", [False]),
                         "the other worker's answer did not land as a second copy")
        self.assertIn(TWICE, self.store.notes_of(self.store.get_post(post["id"])),
                      "the settle overwrote a second-copy note written after its read")


if __name__ == "__main__":
    unittest.main()
