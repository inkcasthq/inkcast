# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #104 (issue #94): backups age out, and a restore re-erases before the app serves.

The restore now reads the erasure records of the database it replaces (``_carry_erasures``). Each test is a way that
step turns a restore into something other than the outcome ``inkcast restore`` promises: done, or refused with a
``restore.refused`` line and exit status 1, the current database untouched."""

import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from inkcast.backup import restore_main
from inkcast.libs.db import Store
from inkcast.libs.db.backup import BackupError, list_backups, restore_backup, take_backup

KEY = "k" * 40


class ADamagedErasureTable(unittest.TestCase):
    """A restore is most needed when the current database is damaged. Here it still opens and its heartbeats read
    (so the running-server guard passes, as on main), but the pages of ``user_erasures`` are damaged: reading them
    raises ``sqlite3.DatabaseError`` ("database disk image is malformed"), which is not the ``OperationalError`` the
    carry step catches. The restore must be refused the documented way, not crash with a traceback."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"
        self.backups = self.dir / "backups"
        store = Store(str(self.db), KEY)
        ada = store.upsert_user("local:ada-review-94", "Ada", None, None)["id"]
        take_backup(self.db, self.backups, version="test")
        self.assertGreater(sum(store.erase_user(ada).values()), 0)
        store.close()
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        root = raw.execute("SELECT rootpage FROM sqlite_master WHERE name = 'user_erasures'").fetchone()[0]
        size = raw.execute("PRAGMA page_size").fetchone()[0]
        raw.close()
        with self.db.open("r+b") as f:                     # damage only the erasure table's page
            f.seek((root - 1) * size)
            f.write(b"\xff" * size)
        check = sqlite3.connect(self.db)
        self.addCleanup(check.close)
        self.assertIsNone(check.execute("SELECT MAX(at) FROM heartbeats").fetchone()[0], "the heartbeat read works")
        with self.assertRaises(sqlite3.DatabaseError):
            check.execute("SELECT * FROM user_erasures").fetchall()
        check.close()
        self.before = self.db.read_bytes()
        self.backup = list_backups(self.backups)[-1]

    def test_the_restore_is_refused_as_a_backup_error_and_the_current_database_is_untouched(self):
        with self.assertRaisesRegex(BackupError, "erasure records"):
            restore_backup(self.backup, self.db, now=time.time() + 3600)
        self.assertEqual(self.db.read_bytes(), self.before, "the current database was not touched")
        self.assertEqual(list(self.dir.glob(".inkcast.db.restoring*")), [], "no half-restored copy left behind")

    def test_inkcast_restore_exits_1_with_restore_refused(self):
        with mock.patch("inkcast.backup._logging"), self.assertLogs("inkcast.backup", "ERROR") as logs:
            code = restore_main([str(self.backup), "--database", str(self.db)])
        self.assertEqual(code, 1)
        self.assertEqual([getattr(r, "event", "") for r in logs.records], ["restore.refused"])
        self.assertEqual(self.db.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
