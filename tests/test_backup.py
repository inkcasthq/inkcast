"""Backups of the SQLite database (libs/db/backup.py, `inkcast backup | restore | backups`): consistent while the
server writes, checked before they count, kept by retention, verified before a restore, and a restore that never
loses the current database or runs under a live server."""

import json
import os
import pathlib
import sqlite3
import stat
import tempfile
import time
import unittest

from inkcast.libs.db import Store
from inkcast.libs.db.backup import BackupError, list_backups, restore_backup, take_backup, verify_backup


class Backups(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.db = self.dir / "data" / "inkcast.db"
        self.db.parent.mkdir()
        self.store = Store(str(self.db), "k" * 40)                    # WAL mode, as the server runs it
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:owner", "owner", None, None)
        self.backups = self.dir / "backups"

    def test_a_backup_of_a_live_database_is_consistent_checked_and_private(self):
        self.store.beat("scheduler")                                  # still in the WAL, not checkpointed
        b = take_backup(self.db, self.backups, version="test")
        self.assertEqual(b.manifest["integrity"], "ok")
        self.assertEqual(b.manifest["rows"]["users"], 1)
        self.assertEqual(b.manifest["rows"]["heartbeats"], 1)        # WAL content is in the copy
        self.assertEqual(verify_backup(b.path)["sha256"], b.manifest["sha256"])
        self.assertFalse(stat.S_IMODE(b.path.stat().st_mode) & 0o007, "a backup is never world-readable")
        # One self-contained file: it opens read-only with nothing beside it.
        self.assertEqual([p.name for p in self.backups.iterdir() if p.suffix not in (".db", ".json")], [])
        conn = sqlite3.connect(f"file:{b.path}?mode=ro", uri=True)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_retention_keeps_the_newest_and_a_failure_removes_nothing(self):
        start = time.time()
        for i in range(3):
            take_backup(self.db, self.backups, keep=2, now=start + i)
        names = [p.name for p in list_backups(self.backups)]
        self.assertEqual(len(names), 2)
        self.assertEqual(names, sorted(names))
        with self.assertRaises(BackupError):
            take_backup(self.dir / "missing.db", self.backups, keep=1, now=start + 9)
        self.assertEqual([p.name for p in list_backups(self.backups)], names)
        self.assertEqual(list(self.backups.glob(".*.partial")), [])   # no half-written file left behind

    def test_a_changed_backup_is_refused(self):
        b = take_backup(self.db, self.backups)
        with b.path.open("ab") as f:
            f.write(b"\0")
        with self.assertRaisesRegex(BackupError, "checksum"):
            verify_backup(b.path)
        with self.assertRaisesRegex(BackupError, "checksum"):
            restore_backup(b.path, self.db, force=True)

    def test_restore_refuses_a_live_server_and_keeps_the_previous_database(self):
        b = take_backup(self.db, self.backups)
        self.store.upsert_user("local:second", "second", None, None)   # a change after the backup
        self.store.beat("scheduler")                                   # the server looks alive
        with self.assertRaisesRegex(BackupError, "looks alive"):
            restore_backup(b.path, self.db)
        self.store.close()
        aside = restore_backup(b.path, self.db, now=time.time() + 3600)  # an hour later: the heartbeat is stale
        self.assertTrue(aside.exists() and aside.name.startswith("inkcast.db.before-restore-"))
        restored = sqlite3.connect(self.db)
        self.addCleanup(restored.close)
        self.assertEqual(restored.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)  # as of the backup
        previous = sqlite3.connect(aside)
        self.addCleanup(previous.close)
        self.assertEqual(previous.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)  # nothing was lost
        self.assertFalse(self.db.with_name("inkcast.db-wal").exists(), "the old WAL went aside with its file")

    def test_the_commands(self):
        from inkcast.launcher import main
        env = {"INKCAST_DATABASE": str(self.db), "INKCAST_BACKUP_DIR": str(self.backups), "INKCAST_LOG_FORMAT": "json"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        with self.assertRaises(SystemExit) as done:
            main(["backup", "--keep", "3"])
        self.assertEqual(done.exception.code, 0)
        self.assertEqual(len(list_backups(self.backups)), 1)
        manifest = json.loads(list_backups(self.backups)[0].with_suffix(".json").read_text())
        self.assertEqual(manifest["file"], list_backups(self.backups)[0].name)
        self.store.beat("scheduler")
        with self.assertRaises(SystemExit) as refused:
            main(["restore", "--latest", str(self.backups)])
        self.assertEqual(refused.exception.code, 1)                    # the server looks alive
        with self.assertRaises(SystemExit) as forced:
            main(["restore", "--latest", str(self.backups), "--force"])
        self.assertEqual(forced.exception.code, 0)
        with self.assertRaises(SystemExit) as empty:
            main(["restore", "--latest", str(self.dir / "nowhere")])
        self.assertEqual(empty.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
