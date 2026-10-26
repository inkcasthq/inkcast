# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #113 (PR #167): re-erasure through ``users.erasure_hash``.

A key rotation after the upgrade: a person from before the column has their hash filled under the key of the day
at the first open (``backfill_erasure_hashes``), the key is rotated, then they are erased. The record carries the
stored hash, under the old key. A backup from before the upgrade has no column: restored, its rows are filled under
the key current then, the new one, and the stored hash in the record matches nothing. Before #167 the record was
the hash under the key current at erasure, and the re-erasure hashed every row under the current key: they matched.
Still one audit row per erasure: the hash under today's key is kept beside it, and carried by a restore."""

import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest

from inkcast.libs.db import Store
from inkcast.libs.db.backup import restore_backup, take_backup
from inkcast.libs.db.erasure import SCHEMA as ERASURE_SCHEMA

OLD_KEY, NEW_KEY = "o" * 40, "n" * 40
# users as every database from before #113 has it: no erasure_hash column.
OLD_USERS = ("CREATE TABLE users (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, display TEXT NOT NULL, email TEXT,"
             " name TEXT, created_at REAL NOT NULL, last_login_at REAL NOT NULL, disabled_at REAL, deleted_at REAL,"
             " held_at REAL)")


def database_from_before_the_column(path: pathlib.Path, people: tuple[str, ...]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(OLD_USERS)
    conn.executemany("INSERT INTO users VALUES (?, ?, ?, NULL, NULL, 1, 1, NULL, NULL, NULL)",
                     [(f"usr_{name}", f"oidc:x#{name}", name) for name in people])
    conn.executescript(ERASURE_SCHEMA)
    conn.commit()
    conn.close()


class AKeyRotationAfterTheUpgrade(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"
        self.backups = self.dir / "backups"

    def test_a_person_restored_from_a_backup_before_the_column_is_re_erased(self):
        database_from_before_the_column(self.db, ("leaver", "stayer"))
        take_backup(self.db, self.backups, version="test")            # before the upgrade: no column
        Store(str(self.db), OLD_KEY).close()                           # the upgrade: rows filled under the old key
        store = Store(str(self.db), NEW_KEY)                           # INKCAST_SECRET_KEY rotated, then they left
        self.assertGreater(sum(store.erase_user("usr_leaver").values()), 0)
        audit = store.user_erasures()
        self.assertEqual(len(audit), 1, "one audit row per erasure, rotation or not")
        store.close()
        restore_backup(sorted(self.backups.glob("*.db"))[-1], self.db, now=time.time() + 3600)
        store = Store(str(self.db), NEW_KEY)
        self.addCleanup(store.close)
        self.assertEqual(list(store.reapply_erasures()), ["usr_leaver"], "restored under the key they were erased under")
        self.assertIsNone(store.get_user("usr_leaver"))
        self.assertIsNotNone(store.get_user("usr_stayer"), "everyone else is kept")
        self.assertEqual(store.user_erasures(), audit, "the carried record, and no second audit row")


if __name__ == "__main__":
    unittest.main()
