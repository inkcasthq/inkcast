# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #113: re-erasure at start-up costs the matches, not every user.

``Store.reapply_erasures`` (issue #94) runs in every process that opens the store. On main, once any erasure existed,
it hashed every user id under the erasure key: 3.65 s and +169 MiB per process at 1M users, against a 256Mi limit in
split mode. Now each person's hash is stored with their row (``users.erasure_hash``, written at creation, indexed, and
filled once for the rows from before it), and the re-erasure joins ``users`` against ``user_erasures`` on it.

The cost is counted in SQLite VM steps (deterministic, unlike wall time) and in calls of ``user_hash``: a thousand
users with one erasure must cost what ten do. An existing database gains the column, filled, and re-erases as before;
so does a database restored from a backup, including one taken before the column existed."""

import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.db.backup import restore_backup, take_backup
from inkcast.libs.db.erasure import SCHEMA as ERASURE_SCHEMA, USER_TABLES, user_hash
from inkcast.libs.secrets import derive_key
from test_units import UserErasureTests

KEY = "k" * 40
SALT = derive_key(KEY, "erasure")
# The users table as every database from before this change has it: the columns a CREATE TABLE from before the sync
# shipped, then the three ``COLUMNS`` added since, in the order ALTER TABLE added them.
OLD_USERS = ("CREATE TABLE users (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, display TEXT NOT NULL, email TEXT,"
             " name TEXT, created_at REAL NOT NULL, last_login_at REAL NOT NULL, disabled_at REAL, deleted_at REAL,"
             " held_at REAL)")


def old_database(path: pathlib.Path, people: tuple[str, ...], erased: tuple[str, ...] = ()) -> None:
    """A database from before this change: ``users`` without the hash column, and an audit row under this
    deployment's key for each of ``erased`` (who still have their row, as after a restore)."""
    conn = sqlite3.connect(path)
    conn.execute(OLD_USERS)
    conn.executemany("INSERT INTO users VALUES (?, ?, ?, NULL, NULL, 1, 1, NULL, NULL, NULL)",
                     [(f"usr_{name}", f"oidc:x#{name}", name) for name in people])
    conn.executescript(ERASURE_SCHEMA)
    conn.executemany("INSERT INTO user_erasures VALUES (?, ?, ?)",
                     [(user_hash(SALT, f"usr_{name}"), 1_700_000_000.0, '{"users": 1}') for name in erased])
    conn.commit()
    conn.close()


def stored_hashes(store: Store) -> dict[str, str | None]:
    return {r["id"]: r["erasure_hash"] for r in store._all("SELECT id, erasure_hash FROM users ORDER BY id")}


class TheCost(unittest.TestCase):
    """One person erased and back (as a restore brings them), among n people who were not."""

    def measure(self, n_people: int) -> tuple[int, int, list[str]]:
        """VM steps and ``user_hash`` calls that ``reapply_erasures`` costs; the ids it erased."""
        store = Store(":memory:", KEY)
        self.addCleanup(store.close)
        people = [store.upsert_user(f"oidc:https://kc/realms/i#p{i}", f"p{i}", None, None)["id"] for i in range(n_people)]
        ada = people[n_people // 2]
        row = store._one("SELECT * FROM users WHERE id = ?", ada)
        self.assertGreater(sum(store.erase_user(ada).values()), 0)
        columns = ", ".join(row.keys())                      # her row back, as it was: a backup from before the erasure
        store._exec(f"INSERT INTO users ({columns}) VALUES ({', '.join('?' * len(row))})", *tuple(row))
        steps = [0]
        store._conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1), 1)
        self.addCleanup(store._conn.set_progress_handler, None, 0)
        with mock.patch("inkcast.libs.db.erasure.user_hash", wraps=user_hash) as hashed:
            erased = store.reapply_erasures()
        store._conn.set_progress_handler(None, 0)
        self.assertEqual(store._one("SELECT COUNT(*) AS n FROM users")["n"], n_people - 1, "everyone else is kept")
        return steps[0], hashed.call_count, list(erased)

    def test_a_thousand_people_with_one_erasure_cost_what_ten_do(self):
        small_steps, small_hashes, small_erased = self.measure(10)
        large_steps, large_hashes, large_erased = self.measure(1000)
        self.assertEqual((len(small_erased), len(large_erased)), (1, 1), "the one person who came back, each time")
        self.assertGreater(small_steps, 0)
        self.assertLess(large_steps, small_steps * 2,
                        f"{small_steps} VM steps for 10 people, {large_steps} for 1000: the work grows with the users")
        self.assertLessEqual(large_hashes, 1, f"{large_hashes} hashes computed for 1000 people with one erasure: every "
                                              "user id is hashed; the stored hash is what the join is for")
        self.assertLessEqual(large_hashes, small_hashes)


class AnExistingDatabase(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"

    def test_gains_the_column_filled_and_indexed_and_still_re_erases(self):
        old_database(self.db, ("ada", "bob", "carol"), erased=("ada",))
        with mock.patch("inkcast.libs.db.erasure.user_hash", wraps=user_hash) as hashed:
            store = Store(str(self.db), KEY)
        self.addCleanup(store.close)
        self.assertEqual(hashed.call_count, 3, "one hash per row from before the column, at the first open")
        self.assertEqual(stored_hashes(store), {f"usr_{name}": user_hash(SALT, f"usr_{name}") for name in ("ada", "bob", "carol")})
        self.assertIn("users_erasure_hash", {r["name"] for r in store._all("PRAGMA index_list(users)")}, "indexed")
        done = store.reapply_erasures()
        self.assertEqual(list(done), ["usr_ada"])
        self.assertEqual(done["usr_ada"]["users"], 1)
        self.assertEqual(sorted(stored_hashes(store)), ["usr_bob", "usr_carol"], "the others are kept")
        self.assertEqual(len(store.user_erasures()), 1, "no second audit row")
        self.assertEqual(store.reapply_erasures(), {})
        store.close()
        with mock.patch("inkcast.libs.db.erasure.user_hash", wraps=user_hash) as hashed:
            again = Store(str(self.db), KEY)                 # opening again: nothing to fill, nothing hashed
        self.addCleanup(again.close)
        self.assertEqual(hashed.call_count, 0)
        self.assertEqual(again.reapply_erasures(), {})

    def test_a_row_written_without_the_hash_is_filled_at_the_next_open(self):
        """A process from before this change, during a rolling update, inserts people without the hash."""
        store = Store(str(self.db), KEY)
        ada = store.upsert_user("oidc:x#ada", "ada", None, None)["id"]
        self.assertEqual(stored_hashes(store)[ada], user_hash(SALT, ada), "written at creation")
        store._exec("INSERT INTO users (id, identity, display, created_at, last_login_at) VALUES ('usr_old', 'oidc:x#old', 'old', 1, 1)")
        self.assertIsNone(stored_hashes(store)["usr_old"])
        store._exec("INSERT INTO user_erasures VALUES (?, ?, ?)", user_hash(SALT, "usr_old"), time.time(), '{"users": 1}')
        store.close()
        store = Store(str(self.db), KEY)
        self.addCleanup(store.close)
        self.assertEqual(stored_hashes(store), {ada: user_hash(SALT, ada), "usr_old": user_hash(SALT, "usr_old")})
        self.assertEqual(list(store.reapply_erasures()), ["usr_old"])

    def test_the_fill_reads_the_rows_in_batches(self):
        """A million rows from before the column must not be read into memory at once; a batch is its own
        transaction, so a start cut short leaves the rest to the next one."""
        old_database(self.db, tuple(f"p{i:03}" for i in range(25)))
        with mock.patch.object(Store, "ERASURE_HASH_BATCH", 7):
            store = Store(str(self.db), KEY)
        self.addCleanup(store.close)
        self.assertEqual(stored_hashes(store), {f"usr_p{i:03}": user_hash(SALT, f"usr_p{i:03}") for i in range(25)})


class ARestoredDatabase(unittest.TestCase):
    seed, rows = UserErasureTests.seed, staticmethod(UserErasureTests.rows)
    PAYLOAD = UserErasureTests.PAYLOAD

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"
        self.backups = self.dir / "backups"

    def counts(self, store, uid):
        return {table: self.rows(store, table, condition, uid) for table, condition in USER_TABLES}

    def newest_backup(self) -> pathlib.Path:
        return sorted(self.backups.glob("*.db"))[-1]

    def test_a_backup_from_before_the_erasure_is_re_erased_through_the_stored_hash(self):
        from inkcast.libs.runtime import open_store
        store = Store(str(self.db), KEY)
        ada, _ = self.seed(store, "ada")
        bob, _ = self.seed(store, "bob")
        bob_rows = self.counts(store, bob)
        take_backup(self.db, self.backups, version="test")
        self.assertGreater(sum(store.erase_user(ada).values()), 0)
        audit = store.user_erasures()
        store.close()
        restore_backup(self.newest_backup(), self.db, now=time.time() + 3600)
        config = Config(public_url="http://localhost:8787", secret_key=KEY, database_path=str(self.db), login="password",
                        password="p" * 12)
        with mock.patch("inkcast.libs.db.erasure.user_hash", wraps=user_hash) as hashed, \
                self.assertLogs("inkcast.runtime", level="WARNING") as logs:
            store = open_store(config)
        self.addCleanup(store.close)
        self.assertEqual(hashed.call_count, 0, "the backup's rows carry their hashes: nothing to fill, nothing hashed")
        self.assertEqual(set(self.counts(store, ada).values()), {0}, "erased again")
        self.assertEqual(self.counts(store, bob), bob_rows)
        self.assertEqual(store.user_erasures(), audit, "no second audit row")
        self.assertEqual([getattr(r, "user_id", "") for r in logs.records if getattr(r, "event", "") == "identity.erasure.reapplied"],
                         [ada])

    def test_a_backup_from_before_the_column_is_filled_and_re_erased(self):
        """The backup predates this change (no column, no hashes); the audit row carried into it is under this key."""
        old_database(self.db, ("ada", "bob"))
        take_backup(self.db, self.backups, version="test")                 # the old backup
        store = Store(str(self.db), KEY)                                   # the deployment upgraded, then she left
        self.assertEqual(stored_hashes(store)["usr_ada"], user_hash(SALT, "usr_ada"))
        self.assertGreater(sum(store.erase_user("usr_ada").values()), 0)
        audit = store.user_erasures()
        store.close()
        restore_backup(self.newest_backup(), self.db, now=time.time() + 3600)
        raw = sqlite3.connect(self.db)
        self.assertNotIn("erasure_hash", [c[1] for c in raw.execute("PRAGMA table_info(users)")], "the restored copy predates it")
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM user_erasures").fetchone()[0], 1, "the audit row was carried")
        raw.close()
        store = Store(str(self.db), KEY)
        self.addCleanup(store.close)
        self.assertEqual(stored_hashes(store), {"usr_ada": user_hash(SALT, "usr_ada"), "usr_bob": user_hash(SALT, "usr_bob")})
        done = store.reapply_erasures()
        self.assertEqual(list(done), ["usr_ada"])
        self.assertEqual(sorted(stored_hashes(store)), ["usr_bob"])
        self.assertEqual(store.user_erasures(), audit)

    def test_a_person_created_under_one_key_and_erased_under_another_is_still_re_erased(self):
        """The record carries the hash stored with the row, so a key rotation between the two doesn't hide her."""
        store = Store(str(self.db), KEY)
        ada, _ = self.seed(store, "ada")
        take_backup(self.db, self.backups, version="test")                 # her rows, hashed under the old key
        store.close()
        store = Store(str(self.db), "r" * 48)                              # INKCAST_SECRET_KEY rotated, then she left
        self.assertGreater(sum(store.erase_user(ada).values()), 0)
        store.close()
        restore_backup(self.newest_backup(), self.db, now=time.time() + 3600)
        store = Store(str(self.db), "r" * 48)
        self.addCleanup(store.close)
        self.assertEqual(list(store.reapply_erasures()), [ada], "erased again despite the rotation")
        self.assertEqual(set(self.counts(store, ada).values()), {0})

    def test_a_person_created_under_one_key_and_erased_under_another_is_still_re_erased(self):
        """The record carries the hash stored with the row, so a key rotation between the two doesn't hide her."""
        store = Store(str(self.db), KEY)
        ada, _ = self.seed(store, "ada")
        take_backup(self.db, self.backups, version="test")                 # her rows, hashed under the old key
        store.close()
        store = Store(str(self.db), "r" * 48)                              # INKCAST_SECRET_KEY rotated, then she left
        self.assertGreater(sum(store.erase_user(ada).values()), 0)
        store.close()
        restore_backup(self.newest_backup(), self.db, now=time.time() + 3600)
        store = Store(str(self.db), "r" * 48)
        self.addCleanup(store.close)
        self.assertEqual(list(store.reapply_erasures()), [ada], "erased again despite the rotation")
        self.assertEqual(set(self.counts(store, ada).values()), {0})


del UserErasureTests                       # helpers only; its tests belong to test_units

if __name__ == "__main__":
    unittest.main()
