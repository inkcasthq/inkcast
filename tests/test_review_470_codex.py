# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Codex's review of PR #491 (issue #470): the wal_pending lines name every connection that can hold the log off, not
readers alone, and SQLite's -1 (another checkpoint held its lock) is not logged as a frame count."""

import sqlite3
import unittest
from unittest import mock

from inkcast.services.scheduler import housekeeping
from test_erasure_wal import APersonWithContent, context, recorded


def quiet_housekeeping(store):
    """Housekeeping with only its checkpoint left to run."""
    with mock.patch("inkcast.services.scheduler.sync_consumers"), mock.patch.object(store, "purge_expired"), \
            mock.patch.object(store, "sweep_orphans", return_value={}):
        with recorded("inkcast.services.scheduler") as records:
            housekeeping(context(store))
    return records


class AWriterBlockingTheCheckpoint(APersonWithContent):
    def test_pending_lines_do_not_name_a_writer_a_reader(self):
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        actual_checkpoint = self.store.checkpoint_wal

        def checkpoint_with_writer():
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO heartbeats (name, at) VALUES (?, ?)", ("held", 1))
            return actual_checkpoint()

        with mock.patch.object(self.store, "checkpoint_wal", side_effect=checkpoint_with_writer):
            with recorded("inkcast.libs.db.erasure") as records:
                self.store.erase_user(self.user["id"])
        (pending,) = records
        self.assertEqual(pending.event, "identity.erasure.wal_pending")
        self.assertGreater(pending.frames, 0)
        self.assertNotIn("readers held", pending.getMessage())
        (pending,) = quiet_housekeeping(self.store)
        self.assertEqual(pending.event, "scheduler.housekeeping.wal_pending")
        self.assertNotIn("readers held", pending.getMessage())
        writer.execute("ROLLBACK")


class AnUncountedLog(APersonWithContent):
    def test_minus_one_is_not_logged_as_a_frame_count(self):
        with mock.patch.object(self.store, "checkpoint_wal", return_value=(1, -1, -1)):
            with recorded("inkcast.libs.db.erasure") as records:
                self.store.checkpoint_after_erasure()
            (erasure,) = records
            (scheduled,) = quiet_housekeeping(self.store)
        for record in (erasure, scheduled):
            with self.subTest(event=record.event):
                self.assertEqual(record.frames, -1)
                self.assertNotIn("-1 frame", record.getMessage())
                self.assertIn("frames not counted", record.getMessage())


if __name__ == "__main__":
    unittest.main()
