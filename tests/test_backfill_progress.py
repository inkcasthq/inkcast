# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #177: the one-off fill of ``users.erasure_hash`` says how far it has got.

The first open after the upgrade to the column (#113), or after a restore of a backup from before it, fills the hash
of every user in batches before ``/livez`` answers, which on a large database can outlast the startup probe. The
batches commit one by one, so a restart mid-way loses nothing, but nothing said so in the log. Now each batch logs the
rows filled so far, of the rows without a hash at this start, and one line says when every user has one; an open with
nothing to fill logs nothing. The transactions themselves are counted in tests/test_fable_review_erasure_hash.py."""

import logging
import pathlib
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from inkcast.libs.db import Store
from inkcast.libs.db.erasure import SCHEMA as ERASURE_SCHEMA, user_hash

KEY = "k" * 40
LOGGER = "inkcast.libs.db.erasure"
# users as every database from before #113 has it: no erasure_hash column.
OLD_USERS = ("CREATE TABLE users (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, display TEXT NOT NULL, email TEXT,"
             " name TEXT, created_at REAL NOT NULL, last_login_at REAL NOT NULL, disabled_at REAL, deleted_at REAL,"
             " held_at REAL)")


def old_database(path: pathlib.Path, people: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(OLD_USERS)
    conn.executemany("INSERT INTO users VALUES (?, ?, ?, NULL, NULL, 1, 1, NULL, NULL, NULL)",
                     [(f"usr_p{i:03}", f"oidc:x#p{i:03}", f"p{i:03}") for i in range(people)])
    conn.executescript(ERASURE_SCHEMA)
    conn.commit()
    conn.close()


def progress(records: list[logging.LogRecord]) -> list[tuple[str, int, int | None, int]]:
    """(event, rows, of, batches) of each backfill line, in order."""
    return [(r.event, r.rows, getattr(r, "of", None), r.batches) for r in records
            if getattr(r, "event", "").startswith("identity.erasure_hash.")]


class TheBackfillSaysHowFarItGot(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"

    def test_a_line_per_batch_with_the_rows_so_far_and_one_when_every_user_has_a_hash(self):
        old_database(self.db, 25)
        with self.assertLogs(LOGGER, level="INFO") as logs, mock.patch.object(Store, "ERASURE_HASH_BATCH", 7):
            Store(str(self.db), KEY).close()
        self.assertEqual(progress(logs.records), [
            ("identity.erasure_hash.backfill", 7, 25, 1),
            ("identity.erasure_hash.backfill", 14, 25, 2),
            ("identity.erasure_hash.backfill", 21, 25, 3),
            ("identity.erasure_hash.backfill", 25, 25, 4),
            ("identity.erasure_hash.backfilled", 25, None, 4),
        ])
        self.assertTrue(all(r.levelno == logging.INFO for r in logs.records), "progress, not a warning")
        for record in logs.records:
            self.assertNotIn("usr_", record.getMessage(), "ids and hashes stay out of the log")
            self.assertNotIn("usr_", str(record.__dict__))
        self.assertIn("25 of the 25", logs.records[3].getMessage())
        self.assertRegex(logs.records[4].getMessage(), r"^Every user has an erasure hash")

    def test_a_start_cut_short_shows_the_batches_it_committed_and_the_next_shows_the_rest(self):
        old_database(self.db, 25)
        calls = [0]

        def hashing(salt, user_id):
            calls[0] += 1
            if calls[0] == 15:                                  # the first row of the third batch
                raise RuntimeError("the pod was stopped mid-backfill")
            return user_hash(salt, user_id)
        with self.assertLogs(LOGGER, level="INFO") as first, mock.patch("inkcast.libs.db.erasure.user_hash", hashing), \
                mock.patch.object(Store, "ERASURE_HASH_BATCH", 7), self.assertRaises(RuntimeError):
            Store(str(self.db), KEY)
        self.assertEqual(progress(first.records), [("identity.erasure_hash.backfill", 7, 25, 1),
                                                   ("identity.erasure_hash.backfill", 14, 25, 2)],
                         "two batches committed and said so; no line claims the fill is done")
        with self.assertLogs(LOGGER, level="INFO") as second, mock.patch.object(Store, "ERASURE_HASH_BATCH", 7):
            Store(str(self.db), KEY).close()
        self.assertEqual(progress(second.records), [("identity.erasure_hash.backfill", 7, 11, 1),
                                                    ("identity.erasure_hash.backfill", 11, 11, 2),
                                                    ("identity.erasure_hash.backfilled", 11, None, 2)],
                         "the next start fills the 11 left, and counts from what it found")

    def test_an_open_with_nothing_to_fill_logs_nothing(self):
        old_database(self.db, 25)
        Store(str(self.db), KEY).close()
        with self.assertNoLogs(LOGGER, level="DEBUG"):
            store = Store(str(self.db), KEY)
        store.close()
        with self.assertNoLogs(LOGGER, level="DEBUG"):
            Store(":memory:", KEY).close()                      # a new database has nobody to fill


if __name__ == "__main__":
    unittest.main()
