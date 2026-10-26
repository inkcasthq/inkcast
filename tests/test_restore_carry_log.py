# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #183, concern 2: a restore says what it carried, aliases included.

A restore carries the erasure records of the database it replaces into the restored copy, and since #113 their
aliases too (``user_erasure_aliases``: the other salted hash an erasure is known by after a key rotation). The log
line counted the records only, so a restore that carried an alias beside a record said "1 record", and one that
carried only aliases (every record already in the backup) said nothing at all. Now the line carries both counts and
is written when either is non-zero."""

import logging
import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest

from inkcast.libs.db import Store
from inkcast.libs.db.backup import _carry_erasures, restore_backup, take_backup
from inkcast.libs.db.erasure import SCHEMA as ERASURE_SCHEMA

KEY, KEY_2 = "k" * 40, "r" * 48
LOGGER = "inkcast.libs.db.backup"


def carried(records: list[logging.LogRecord]) -> list[tuple[int, int]]:
    """(rows, aliases) of each ``restore.erasures.carried`` line."""
    return [(r.rows, getattr(r, "aliases", None)) for r in records if getattr(r, "event", "") == "restore.erasures.carried"]


class TheRestoreSaysWhatItCarried(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"
        self.backups = self.dir / "backups"

    def test_a_record_and_its_alias_are_both_counted(self):
        store = Store(str(self.db), KEY)
        leaver = store.upsert_user("oidc:x#leaver", "leaver", None, None)["id"]
        store.upsert_user("oidc:x#stayer", "stayer", None, None)
        store.close()
        take_backup(self.db, self.backups, version="test")          # before the erasure: no record, no alias
        store = Store(str(self.db), KEY_2)                           # INKCAST_SECRET_KEY rotated, then they left
        self.assertGreater(sum(store.erase_user(leaver).values()), 0)
        self.assertEqual(len(store._all("SELECT * FROM user_erasure_aliases")), 1, "the rotation made an alias")
        store.close()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            restore_backup(sorted(self.backups.glob("*.db"))[-1], self.db, now=time.time() + 3600)
        self.assertEqual(carried(logs.records), [(1, 1)], "one record and one alias carried")
        (line,) = [r for r in logs.records if getattr(r, "event", "") == "restore.erasures.carried"]
        self.assertIn("1 alias", line.getMessage())
        self.assertNotIn(leaver, line.getMessage() + str(line.__dict__), "ids and hashes stay out of the log")
        store = Store(str(self.db), KEY_2)
        self.addCleanup(store.close)
        self.assertEqual(list(store.reapply_erasures()), [leaver], "and the person is erased again")

    def test_a_restore_that_carries_only_aliases_still_says_so(self):
        """The backup already holds every record and not the alias beside one of them (its records were carried by a
        restore from before the alias table): the carry adds the alias and nothing else, and must not be silent."""
        current, incoming = self.dir / "current.db", self.dir / "incoming.db"
        for path in (current, incoming):
            conn = sqlite3.connect(path)
            conn.executescript(ERASURE_SCHEMA)
            conn.execute("INSERT INTO user_erasures VALUES (?, ?, ?)", ("a" * 64, 1_700_000_000.0, "{}"))
            conn.commit()
            conn.close()
        conn = sqlite3.connect(current)
        conn.execute("INSERT INTO user_erasure_aliases VALUES (?, ?)", ("a" * 64, "b" * 64))
        conn.commit()
        conn.close()
        self.assertEqual(_carry_erasures(current, incoming), (0, 1), "no record to carry, one alias")
        conn = sqlite3.connect(incoming)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT user_hash, alias FROM user_erasure_aliases").fetchall(), [("a" * 64, "b" * 64)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_erasures").fetchone()[0], 1, "no second record")
        self.assertEqual(_carry_erasures(current, incoming), (0, 0), "carried once")

    def test_nothing_to_carry_logs_nothing(self):
        store = Store(str(self.db), KEY)
        store.upsert_user("oidc:x#stayer", "stayer", None, None)
        store.close()
        take_backup(self.db, self.backups, version="test")
        with self.assertNoLogs(LOGGER, level="INFO"):
            restore_backup(sorted(self.backups.glob("*.db"))[-1], self.db, now=time.time() + 3600)


if __name__ == "__main__":
    unittest.main()
