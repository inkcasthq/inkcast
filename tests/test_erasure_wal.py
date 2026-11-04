# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""An erased person's bytes leave the files on disk, not just the tables (issue #404, item 1).

``PRAGMA secure_delete`` zeroes what a transaction deletes, but in WAL mode that transaction's pages go to the
write-ahead log: the database file keeps the person's pages until a checkpoint copies the zeroed ones over them, and the
log file keeps every frame it ever held until it is truncated, a checkpoint only letting the writer start over at its
beginning. So the person's marker stayed in ``inkcast.db-wal`` (and, below the automatic checkpoint's 1000 pages, in
``inkcast.db``) through two PASSIVE checkpoints and twenty more writes. Now every erasure ends with a TRUNCATE
checkpoint, and housekeeping runs one every tick for the erasure whose checkpoint met a reader on an older snapshot.
Each test reads the bytes of every file beside the database, never a table."""

import logging
import pathlib
import shutil
import sqlite3
import tempfile
import types
import unittest
import uuid

from inkcast.libs.config import Config, RetentionPolicy
from inkcast.libs.db import Store
from inkcast.libs.db.backup import restore_backup, take_backup
from inkcast.libs.runtime import open_store
from inkcast.services.scheduler import housekeeping

KEY = "k" * 40
PAYLOAD = {"platform": "fake", "title": "T", "payload": {}, "preview": {}}
PENDING = "identity.erasure.wal_pending"


def context(store: Store) -> types.SimpleNamespace:
    """What ``housekeeping`` reads of the scheduler's context."""
    return types.SimpleNamespace(store=store, config=types.SimpleNamespace(retention=RetentionPolicy()))


class APersonWithContent(unittest.TestCase):
    """A person with 20 posts, written just now: their frames are in the write-ahead log, below the automatic
    checkpoint's 1000 pages (every statement appends a frame per page it touches, so 200 posts would cross it), and
    the database file holds nothing of them yet (the issue's case). ``settled`` makes them a day-old person instead: a
    checkpoint has copied their pages into the database file."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"
        self.store = Store(str(self.db), KEY)
        self.addCleanup(lambda: self.store.close())
        self.store.upsert_user("oidc:kc#other", "Other", None, None)        # someone stays: the tables are not dropped
        self.store.checkpoint_wal()
        self.marker = "wal404-" + uuid.uuid4().hex
        self.user = self.store.upsert_user(f"oidc:kc#{self.marker}", self.marker, f"{self.marker}@example.test", self.marker)
        for i in range(20):
            self.store.create_post(group_id=f"g{i}", user_id=self.user["id"], platform="fake", title=f"{self.marker} {i}",
                                   status="scheduled", run_at=4e9, payload={**PAYLOAD, "body": f"{self.marker} " * 20})
        self.assertEqual(self.holding(), ["inkcast.db-wal"])

    def settled(self) -> None:
        self.store.checkpoint_wal()
        self.assertEqual(self.holding(), ["inkcast.db"])

    def holding(self) -> list[str]:
        """Every file beside the database whose bytes hold the marker."""
        return sorted(p.name for p in self.dir.iterdir() if p.is_file() and self.marker.encode() in p.read_bytes())

    def wal_size(self) -> int:
        wal = self.db.with_name("inkcast.db-wal")
        return wal.stat().st_size if wal.exists() else 0

    def pinned_by_a_reader(self) -> sqlite3.Connection:
        """Another process's connection holding a snapshot from before the erasure."""
        reader = sqlite3.connect(self.db)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM users").fetchall()
        return reader

    def erase_with_the_log_pinned(self) -> None:
        with self.assertLogs("inkcast.libs.db.erasure", logging.WARNING) as logs:
            self.assertEqual(self.store.erase_user(self.user["id"])["users"], 1)
        (pending,) = logs.records
        self.assertEqual(pending.event, PENDING)
        self.assertGreater(pending.frames, 0)
        self.assertNotIn(self.marker, pending.getMessage())


class TheErasure(APersonWithContent):
    def test_leaves_no_byte_of_the_person_in_any_file_with_readers_open(self):
        readers = [sqlite3.connect(self.db) for _ in range(3)]         # other processes' connections, open throughout
        for reader in readers:
            self.addCleanup(reader.close)
            reader.execute("SELECT COUNT(*) FROM users").fetchall()      # each has read, and holds no snapshot now
        with self.assertNoLogs("inkcast.libs.db.erasure", logging.WARNING):
            counts = self.store.erase_user(self.user["id"])
        self.assertEqual(counts["posts"], 20)
        self.assertEqual(self.holding(), [], "the database file, its -wal and its -shm hold nothing of the person")
        self.assertEqual(self.wal_size(), 0, "the write-ahead log was cut, not just restarted")
        self.assertEqual(readers[0].execute("SELECT COUNT(*) FROM users").fetchone()[0], 1, "readers go on")

    def test_takes_a_day_old_person_s_pages_out_of_the_database_file_too(self):
        # secure_delete zeroes them in the transaction, but the zeroed pages go to the log: the database file keeps
        # the person's pages until the checkpoint copies the zeroed ones over them.
        self.settled()
        with self.assertNoLogs("inkcast.libs.db.erasure", logging.WARNING):
            self.store.erase_user(self.user["id"])
        self.assertEqual(self.holding(), [])
        self.assertEqual(self.wal_size(), 0)

    def test_a_reader_on_an_older_snapshot_leaves_the_log_to_housekeeping_within_one_tick(self):
        reader = self.pinned_by_a_reader()
        self.erase_with_the_log_pinned()
        self.assertEqual(self.holding(), ["inkcast.db-wal"], "the log keeps the frames the reader's snapshot pins")
        reader.execute("COMMIT")                                          # the reader is done before the next tick
        housekeeping(context(self.store))
        self.assertEqual(self.holding(), [], "one housekeeping tick on: nothing of the person in any file")
        self.assertEqual(self.wal_size(), 0)

    def test_a_reader_on_an_older_snapshot_leaves_a_day_old_person_s_pages_to_housekeeping(self):
        self.settled()
        reader = self.pinned_by_a_reader()
        self.erase_with_the_log_pinned()
        self.assertEqual(self.holding(), ["inkcast.db"], "the database file keeps the pages the snapshot still reads")
        reader.execute("COMMIT")
        housekeeping(context(self.store))
        self.assertEqual(self.holding(), [])

    def test_housekeeping_alone_cuts_the_log_after_plain_writes(self):
        for i in range(20):
            self.store.beat(f"hb{i}")
        self.assertGreater(self.wal_size(), 0)
        housekeeping(context(self.store))
        self.assertEqual(self.wal_size(), 0)


class AReapplyAfterARestore(APersonWithContent):
    def test_leaves_no_byte_of_the_person_in_any_file(self):
        self.settled()
        before = take_backup(self.db, self.dir / "backups", version="test", now=4e9)
        self.assertEqual(self.store.erase_user(self.user["id"])["users"], 1)
        self.store.close()
        restore_backup(before.path, self.db, now=4e9 + 3600)             # the backup from before the erasure comes back
        self.assertIn("inkcast.db", self.holding())
        config = Config(public_url="http://localhost:8787", secret_key=KEY, database_path=str(self.db))
        with self.assertLogs("inkcast.runtime", logging.WARNING) as logs:
            self.store = open_store(config)                              # re-erases before anything serves (#94)
        self.assertEqual([r.event for r in logs.records], ["identity.erasure.reapplied"])
        self.assertEqual([n for n in self.holding() if not n.startswith("inkcast.db.before-restore-")], [],
                         "the restored database, its -wal and its -shm hold nothing of the person; the copy set "
                         "aside does, until it ages out (#112)")


if __name__ == "__main__":
    unittest.main()
