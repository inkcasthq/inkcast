# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Fable review of #113 (PR #167, with PR #175): re-erasure through ``users.erasure_hash``.

Test strengthening, no product change. The previous review noted that the batching test in
tests/test_reapply_erasures_cost.py checks only the outcome, and that its reopen test counts hashes, not transactions.
Here the SQL is traced (``sqlite3.Connection.set_trace_callback``), so what ``backfill_erasure_hashes`` claims is what
is checked: one transaction per batch of at most ``ERASURE_HASH_BATCH`` rows; a start cut short keeps the batches it
committed and the next open fills the rest; once filled, an open costs one indexed read and no transaction; and
``reapply_erasures`` with no record is one read and no write.

The alias (``user_erasure_aliases``) is pinned down too: none without a key rotation, and with one exactly one, naming
the same person under both keys beside the one audit row. A backup from before the alias table existed (a database
from before #113, whose ``user_erasures`` has no alias table beside it, or from before #94, with neither) still gets
the alias carried by a restore and the person re-erased.

Every key-rotation order the record can cover is run as a matrix: a backup taken after the column survives any number
of rotations, before or after the erasure; a backup from before the column survives rotations before the erasure.
The orders it cannot cover are known (#112 item 5: a rotation after the erasure, then a restore of a row that had no
hash in the backup) or reported as a concern with this review, not coded here."""

import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from inkcast.libs.db import Store
from inkcast.libs.db.backup import restore_backup, take_backup
from inkcast.libs.db.erasure import SCHEMA as ERASURE_SCHEMA, user_hash
from inkcast.libs.secrets import derive_key

KEY, KEY_2, KEY_3 = "k" * 40, "r" * 48, "s" * 56
# users as every database from before #113 has it: no erasure_hash column.
OLD_USERS = ("CREATE TABLE users (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, display TEXT NOT NULL, email TEXT,"
             " name TEXT, created_at REAL NOT NULL, last_login_at REAL NOT NULL, disabled_at REAL, deleted_at REAL,"
             " held_at REAL)")
# The erasure audit as a database from before #113 has it: the records, no alias table beside them.
OLD_ERASURES = "CREATE TABLE user_erasures (user_hash TEXT NOT NULL, erased_at REAL NOT NULL, counts TEXT NOT NULL);"
PROBE = "SELECT 1 FROM users WHERE erasure_hash IS NULL LIMIT 1"


def old_database(path: pathlib.Path, people: tuple[str, ...], erasures: str | None = ERASURE_SCHEMA) -> None:
    """A database from before #113: ``users`` without the hash column, and the erasure tables as ``erasures`` creates
    them (today's two, the one from before #113, or none at all for a database from before #94)."""
    conn = sqlite3.connect(path)
    conn.execute(OLD_USERS)
    conn.executemany("INSERT INTO users VALUES (?, ?, ?, NULL, NULL, 1, 1, NULL, NULL, NULL)",
                     [(f"usr_{name}", f"oidc:x#{name}", name) for name in people])
    if erasures:
        conn.executescript(erasures)
    conn.commit()
    conn.close()


def hashes(path: pathlib.Path) -> dict[str, str | None]:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT id, erasure_hash FROM users ORDER BY id").fetchall())
    finally:
        conn.close()


class Traced:
    """Every SQL statement the connections opened inside ``with`` ran, in order (each row of an executemany is one)."""

    def __init__(self):
        self.statements: list[str] = []
        self._real = sqlite3.connect

    def _connect(self, *args, **kwargs):
        conn = self._real(*args, **kwargs)
        conn.set_trace_callback(self.statements.append)
        return conn

    def __enter__(self):
        self._patch = mock.patch("sqlite3.connect", self._connect)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    def transactions(self) -> list[list[str]]:
        """The statements of each BEGIN ... COMMIT, in order."""
        out, current = [], None
        for statement in self.statements:
            if statement.startswith("BEGIN"):
                current = []
            elif statement == "COMMIT":
                out.append(current)
                current = None
            elif current is not None:
                current.append(statement)
        return out


class TheBackfillTraced(unittest.TestCase):
    """What ``backfill_erasure_hashes`` promises, checked statement by statement."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"
        self.people = tuple(f"p{i:03}" for i in range(25))

    def test_each_batch_is_one_transaction_of_at_most_the_batch_size(self):
        old_database(self.db, self.people)
        with Traced() as traced, mock.patch.object(Store, "ERASURE_HASH_BATCH", 7):
            store = Store(str(self.db), KEY)
        store.close()
        self.assertEqual(hashes(self.db), {f"usr_{n}": user_hash(derive_key(KEY, "erasure"), f"usr_{n}") for n in self.people})
        # The columns the old database lacks are added first, in one write transaction of their own (issue #176).
        upgrade, *batches = traced.transactions()
        self.assertIn("ALTER TABLE users ADD COLUMN erasure_hash TEXT", upgrade)
        self.assertFalse([s for s in upgrade if not s.startswith(("ALTER TABLE", "PRAGMA table_info"))], upgrade)
        self.assertEqual(len(batches), 4, "25 rows in batches of 7: four transactions, not one")
        self.assertEqual([sum(s.startswith("UPDATE users SET erasure_hash") for s in b) for b in batches], [7, 7, 7, 4],
                         "each transaction fills at most one batch")
        for batch in batches:
            self.assertEqual(len([s for s in batch if s.startswith("SELECT id FROM users WHERE erasure_hash IS NULL LIMIT")]), 1,
                             "each batch reads at most the batch, inside its own transaction")
        self.assertEqual(traced.statements.count(PROBE), 5, "a probe before each batch, and one more that finds nothing left")
        self.assertNotIn("ROLLBACK", traced.statements)
        self.assertTrue(all(s.startswith("BEGIN IMMEDIATE") for s in traced.statements if s.startswith("BEGIN")),
                        "the write lock is taken up front, as every write transaction here does")

    def test_a_start_cut_short_keeps_the_batches_committed_and_the_next_open_fills_the_rest(self):
        old_database(self.db, self.people)
        calls = [0]

        def hashing(salt, user_id):
            calls[0] += 1
            if calls[0] == 15:                                  # the first row of the third batch
                raise RuntimeError("the pod was stopped mid-backfill")
            return user_hash(salt, user_id)
        with mock.patch("inkcast.libs.db.erasure.user_hash", hashing), mock.patch.object(Store, "ERASURE_HASH_BATCH", 7), \
                self.assertRaises(RuntimeError):
            Store(str(self.db), KEY)
        filled = hashes(self.db)
        self.assertEqual(sum(v is not None for v in filled.values()), 14, "the two batches done are committed; the third rolled back")
        self.assertEqual(sum(v is None for v in filled.values()), 11)
        with Traced() as traced, mock.patch.object(Store, "ERASURE_HASH_BATCH", 7):
            store = Store(str(self.db), KEY)
        store.close()
        self.assertEqual([sum(s.startswith("UPDATE users") for s in b) for b in traced.transactions()], [7, 4],
                         "the next open fills exactly the rest")
        self.assertEqual(hashes(self.db), {f"usr_{n}": user_hash(derive_key(KEY, "erasure"), f"usr_{n}") for n in self.people})

    def test_once_filled_an_open_is_one_indexed_read_and_no_transaction(self):
        old_database(self.db, self.people)
        Store(str(self.db), KEY).close()
        with Traced() as traced:
            store = Store(str(self.db), KEY)
        self.addCleanup(store.close)
        self.assertEqual(traced.transactions(), [], "nothing to fill: no write transaction, no lock taken")
        self.assertEqual([s for s in traced.statements if s.startswith(("BEGIN", "UPDATE", "INSERT", "ALTER"))], [])
        self.assertEqual(traced.statements.count(PROBE), 1, "one probe")
        plan = " ".join(r[3] for r in store._conn.execute(f"EXPLAIN QUERY PLAN {PROBE}"))
        self.assertIn("INDEX users_erasure_hash", plan, f"the probe must come from the index, not a scan: {plan}")
        self.assertNotIn("SCAN", plan, plan)

    def test_reapply_with_no_record_is_one_read_and_no_write(self):
        store = Store(str(self.db), KEY)
        self.addCleanup(store.close)
        for name in ("ada", "bob"):
            store.upsert_user(f"oidc:x#{name}", name, None, None)
        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        self.assertEqual(store.reapply_erasures(), {})
        store._conn.set_trace_callback(None)
        self.assertEqual(len(statements), 1, statements)
        self.assertTrue(statements[0].startswith("SELECT id FROM users WHERE erasure_hash IN"), statements)
        plan = " ".join(r[3] for r in store._conn.execute("EXPLAIN QUERY PLAN " + statements[0]))
        self.assertRegex(plan, r"SEARCH (TABLE )?users USING (COVERING )?INDEX users_erasure_hash", plan)


class TheAlias(unittest.TestCase):
    """``user_erasure_aliases``: one row, for the same person, only when a rotation made the two hashes differ."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"
        self.backups = self.dir / "backups"

    def aliases(self, store: Store) -> list[tuple[str, str]]:
        return [tuple(r) for r in store._all("SELECT user_hash, alias FROM user_erasure_aliases")]

    def test_none_without_a_rotation_and_exactly_one_for_the_same_person_with_one(self):
        salt, salt_2 = derive_key(KEY, "erasure"), derive_key(KEY_2, "erasure")
        store = Store(str(self.db), KEY)
        ada = store.upsert_user("oidc:x#ada", "ada", None, None)["id"]
        bob = store.upsert_user("oidc:x#bob", "bob", None, None)["id"]
        self.assertGreater(sum(store.erase_user(ada).values()), 0)
        self.assertEqual([r["user_hash"] for r in store.user_erasures()], [user_hash(salt, ada)])
        self.assertEqual(self.aliases(store), [], "the stored hash and today's are the same: nothing to add")
        store.close()
        store = Store(str(self.db), KEY_2)                            # INKCAST_SECRET_KEY rotated
        self.addCleanup(store.close)
        self.assertGreater(sum(store.erase_user(bob).values()), 0)
        self.assertEqual([r["user_hash"] for r in store.user_erasures()], [user_hash(salt, ada), user_hash(salt, bob)],
                         "one audit row per erasure, carrying the hash stored with the row (the old key's)")
        self.assertEqual(self.aliases(store), [(user_hash(salt, bob), user_hash(salt_2, bob))],
                         "one alias: the same person under the key current at the erasure")
        self.assertNotIn(user_hash(salt_2, ada), {h for pair in self.aliases(store) for h in pair}, "nobody else's hash")
        self.assertEqual(set(store.erase_user(bob).values()), {0}, "erasing again changes nothing")
        self.assertEqual(len(self.aliases(store)), 1)
        self.assertEqual(len(store.user_erasures()), 2)

    def check_restore_of_an_old_backup(self, erasures: str | None) -> None:
        old_database(self.db, ("leaver", "stayer"), erasures)
        take_backup(self.db, self.backups, version="test")            # the old backup: no alias table (or no audit at all)
        Store(str(self.db), KEY).close()                               # upgraded: filled under the old key
        store = Store(str(self.db), KEY_2)                             # rotated, then they left
        self.assertGreater(sum(store.erase_user("usr_leaver").values()), 0)
        audit, aliases = store.user_erasures(), self.aliases(store)
        self.assertEqual((len(audit), len(aliases)), (1, 1))
        store.close()
        restore_backup(sorted(self.backups.glob("*.db"))[-1], self.db, now=time.time() + 3600)
        raw = sqlite3.connect(self.db)
        self.assertEqual(raw.execute("SELECT user_hash, alias FROM user_erasure_aliases").fetchall(), aliases,
                         "the alias table was made in the restored copy and the alias carried")
        raw.close()
        store = Store(str(self.db), KEY_2)
        self.addCleanup(store.close)
        self.assertEqual(list(store.reapply_erasures()), ["usr_leaver"])
        self.assertIsNone(store.get_user("usr_leaver"))
        self.assertIsNotNone(store.get_user("usr_stayer"))
        self.assertEqual((store.user_erasures(), self.aliases(store)), (audit, aliases))

    def test_a_backup_from_before_the_alias_table_gets_the_alias_carried(self):
        self.check_restore_of_an_old_backup(OLD_ERASURES)

    def test_a_backup_from_before_any_erasure_table_gets_the_alias_carried(self):
        self.check_restore_of_an_old_backup(None)


class EveryRotationOrder(unittest.TestCase):
    """The matrix: ``keys`` is the key at each step, in order; ``column`` whether the backup was taken after the
    column existed (the person's row carries a hash in it) or before (no column: filled at the restore)."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"
        self.backups = self.dir / "backups"

    def run_order(self, column: bool, create: str, between: tuple[str, ...], erase: str, restore: str) -> bool:
        """A person created (or upgraded) under ``create``, the key rotated through ``between``, erased under
        ``erase``, the backup restored and the store opened under ``restore``. Whether they were erased again."""
        if column:
            store = Store(str(self.db), create)
            uid = store.upsert_user("oidc:x#leaver", "leaver", None, None)["id"]
            store.upsert_user("oidc:x#stayer", "stayer", None, None)
            store.close()
            take_backup(self.db, self.backups, version="test")
        else:
            old_database(self.db, ("leaver", "stayer"))
            take_backup(self.db, self.backups, version="test")
            Store(str(self.db), create).close()
            uid = "usr_leaver"
        for key in between:
            Store(str(self.db), key).close()
        store = Store(str(self.db), erase)
        self.assertGreater(sum(store.erase_user(uid).values()), 0)
        self.assertEqual(len(store.user_erasures()), 1)
        store.close()
        restore_backup(sorted(self.backups.glob("*.db"))[-1], self.db, now=time.time() + 3600)
        store = Store(str(self.db), restore)
        self.addCleanup(store.close)
        done = list(store.reapply_erasures())
        self.assertIsNotNone(store.get_user("usr_stayer") or store.find_user_by_identity("oidc:x#stayer"), "everyone else is kept")
        self.assertEqual(len(store.user_erasures()), 1, "no second audit row")
        return done == [uid] and store.get_user(uid) is None

    def test_a_backup_taken_after_the_column_survives_any_rotation(self):
        for order in ((KEY, (), KEY, KEY), (KEY, (), KEY_2, KEY_2), (KEY, (KEY_2,), KEY_3, KEY_3), (KEY, (), KEY, KEY_2),
                      (KEY, (), KEY_2, KEY_3), (KEY, (KEY_2,), KEY, KEY_3), (KEY, (KEY_2,), KEY_3, KEY)):
            with self.subTest(order=order):
                self.setUp()
                self.assertTrue(self.run_order(True, *order), f"not re-erased in order {order}")

    def test_a_backup_taken_before_the_column_survives_rotations_before_the_erasure(self):
        for order in ((KEY, (), KEY, KEY), (KEY, (), KEY_2, KEY_2), (KEY, (KEY_2,), KEY_3, KEY_3), (KEY, (KEY_2,), KEY, KEY)):
            with self.subTest(order=order):
                self.setUp()
                self.assertTrue(self.run_order(False, *order), f"not re-erased in order {order}")


if __name__ == "__main__":
    unittest.main()
