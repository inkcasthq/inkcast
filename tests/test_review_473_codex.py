# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Codex's review of PR #500 (issue #473), the findings the integrator took: ``update``'s fenced loop ends, and
``settle_edited`` is fenced on the payload it checked."""

import json
import unittest
from unittest import mock

from inkcast.libs.pipeline import _edited
from inkcast.services.publishing.sync import stored_hash

from test_review_sync_427_456 import TWICE
from test_sync_edit_during_send_392 import Editable
from test_sync_edited_note_427 import EditedWhileOut
from test_sync_error_after_post import SyncCase


class FindingUpdatesLoopEnds(SyncCase):
    platform_class = Editable

    def test_a_row_that_keeps_changing_ends_the_update_with_an_error(self):
        """With no cap, a row whose notes another writer changes before every write held ``update`` in its loop, and the
        event loop with it (nothing in the loop awaits): measured on c0d45d98, it never ended. It ends after
        UPDATE_FENCE_MISSES, the last note written kept and the PUT made once."""
        first = self.sync()
        real_update, misses = self.store.update_post, []

        def another_writer_before_each_write(post_id, *args, **fields):
            if "payload" in fields:
                note = f"concurrent note {len(misses) + 1}"
                real_update(post_id, error=note)
                misses.append(note)
                if len(misses) == 12:
                    self.fail("update kept retrying an existing row under continuous contention")
            return real_update(post_id, *args, **fields)

        with mock.patch.object(self.store, "update_post", another_writer_before_each_write):
            with self.assertRaisesRegex(RuntimeError, "sync it again"):
                self.sync(body="Edited.")
        self.assertEqual(len(misses), 10)
        self.assertEqual(self.store.get_post(first.post_id)["error"], misses[-1])
        self.assertEqual(self.platform.updated, [("1", "Edited.")])


class FindingSettleIsFencedOnThePayload(EditedWhileOut):
    platform_class = Editable

    def test_a_payload_changed_between_settles_read_and_its_write_keeps_the_note(self):
        """``settle_edited`` fenced its write on status, attempts and notes: a late same-copy success writing other
        content with the same note, between its read and its write, left the row holding content the file does not,
        and the note of the change still owed cleared (measured on c0d45d98)."""
        when, post = self.success_lands_after(lambda when, post: self.sync_async(when, "Edited."))
        row = self.store.get_post(post["id"])
        note = _edited(self.platform, row["result_url"], row["remote_id"])
        self.assertIn(note, self.store.notes_of(row))
        original = json.loads(row["payload"])
        changed = dict(original, title="Another title")
        real_update, landed = self.store.update_post, []

        def same_copy_before_settle(post_id, *args, **fields):
            if "if_attempts" in fields and not landed:
                landed.append(self.store.record_success(
                    post_id, held=row["attempts"], status="published", result_url=row["result_url"],
                    remote_id=row["remote_id"], sent=changed, notes=[], second_copy=str,
                    both_copies=lambda _row: TWICE, edited=note))
            return real_update(post_id, *args, **fields)

        with mock.patch.object(self.store, "update_post", same_copy_before_settle):
            result = self.sync(when, body="Body.")
        self.assertEqual(result.action, "unchanged")
        self.assertEqual([written for written, _ in landed], [True])
        after = self.store.get_post(post["id"])
        self.assertNotEqual(stored_hash(json.loads(after["payload"])), self.entry()["content_hash"])
        self.assertIn(note, self.store.notes_of(after), "settle cleared a note after the row stopped holding the file")


if __name__ == "__main__":
    unittest.main()
