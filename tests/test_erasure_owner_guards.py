# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #219: the owner guards across releases, and the orphan sweep across tables and processes.

Found by the review of PR #208 (issue #196):

* ``install_owner_guards`` dropped every ``owner_guard_*`` trigger it had not generated, so a pod of the previous release
  restarting during an upgrade, or a rollback, removed the newer release's guard on a table the older one did not know,
  and that table was unguarded until a newer pod opened. Now a guard is dropped only by a release that knows its table
  (read from the schema, ``sqlite_master.tbl_name``): on a table both releases guard the last opener's shape wins, as
  before; on a table only the newer release knows, its guard stays.
* ``sweep_orphans`` ran once per database, so a table added to ``OWNERS`` later was never swept on a database already
  marked. The marker now lists the tables swept; a marker from before the list (``"1"``) stands for the tables of the
  release that wrote it, and a table not in it is swept once.
* A batch another process deleted between the sweep's read and its delete ended that table's sweep, with the database
  marked done. Now the next read says what is left.
* ``authz_write`` let the guard skip a tuple for a ``user:`` subject with no ``users`` row while the rest of the write
  landed. Now the whole write is refused (``NoSuchUser``, an ``InvalidRequest`` through the local engine): all or
  nothing, as the engine promises, and nothing of an erased person's lands.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from inkcast.libs.authz import InvalidRequest, LocalEngine, Tuple
from inkcast.libs.db import Store
from inkcast.libs.db.backup import restore_backup, take_backup
from inkcast.libs.db.erasure import ORPHANS_SWEPT, OWNERS, USER_TABLES, orphan_condition, owner_guards
from test_erasure_orphans import KEY, ZEROS, TwoConnections, orphan, rows, rows_of, seed
from test_fable_review_erasure_hash import Traced

TUPLES = dict(USER_TABLES)["authz_tuples"]


def without(table: str) -> tuple:
    """``OWNERS`` as a release that does not know ``table`` has it."""
    return tuple(owner for owner in OWNERS if owner[0] != table)


def on(table: str, names) -> set[str]:
    return {name for name in names if name.startswith(f"owner_guard_{table}_")}


class AcrossReleases(unittest.TestCase):
    """Two releases whose ``OWNERS`` differ open one database: a pod of the previous release restarting during the
    upgrade, a rollback, or a release that took a table out of ``OWNERS``."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = str(self.dir / "inkcast.db")

    def guards(self) -> dict[str, str]:
        """``{trigger: table}`` as the database has them."""
        conn = sqlite3.connect(self.path)
        try:
            return dict(conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'").fetchall())
        finally:
            conn.close()

    def test_an_older_open_leaves_a_newer_guard_on_a_table_it_does_not_know_and_replaces_its_own(self):
        """The newer release guards ``authz_tuples`` and changed the body of the guard on ``posts``; the older one
        knows ``posts`` (with its own body) and not ``authz_tuples``."""
        Store(self.path, KEY).close()                                    # the first pod on the new image opened
        newer = dict(owner_guards())
        older_owners = tuple(("posts", column, person, "{row}.status != 'draft'") if table == "posts" else
                             (table, column, person, where) for table, column, person, where in without("authz_tuples"))
        with mock.patch("inkcast.libs.db.erasure.OWNERS", older_owners):
            older = dict(owner_guards())
            with Traced() as traced:
                Store(self.path, KEY).close()                            # a pod on the previous image restarted
        self.assertEqual(set(older) - set(newer), on("posts", older), "the one body that differs is posts'")
        creates = [s for s in traced.statements if s.startswith("CREATE TRIGGER")]
        drops = [s for s in traced.statements if s.startswith("DROP TRIGGER")]
        self.assertEqual(sorted(creates), sorted(older[name] for name in on("posts", older)), "its own shape on posts")
        self.assertEqual(sorted(drops), sorted(f"DROP TRIGGER IF EXISTS {name}" for name in on("posts", newer)),
                         "the newer shape on posts, a table it knows, goes; nothing on authz_tuples, which it does not know")
        self.assertLess(traced.statements.index(creates[0]), traced.statements.index(drops[0]), "never a moment without one")
        have = self.guards()
        self.assertEqual(set(have), (set(newer) - on("posts", newer)) | set(older))
        self.assertEqual({have[name] for name in on("authz_tuples", have)}, {"authz_tuples"})
        # The newer release's guard still binds every connection, the older pod's included.
        conn = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("INSERT INTO authz_tuples VALUES ('user:nobody', 'owner', 'organization:acme',"
                                      " 'organization', 0)").rowcount, 0, "a tuple for a person with no users row is refused")
        # The newer release opening again takes posts back and touches nothing else: last opener wins on a known table.
        with Traced() as traced:
            Store(self.path, KEY).close()
        self.assertEqual(sorted(s for s in traced.statements if s.startswith("CREATE TRIGGER")),
                         sorted(newer[name] for name in on("posts", newer)))
        self.assertEqual(sorted(s for s in traced.statements if s.startswith("DROP TRIGGER")),
                         sorted(f"DROP TRIGGER IF EXISTS {name}" for name in on("posts", older)))
        self.assertEqual(set(self.guards()), set(newer))

    def test_a_release_that_took_a_table_out_of_owners_leaves_its_guard_which_keeps_the_rule(self):
        """The reverse case. A table leaves ``OWNERS`` when it leaves ``USER_TABLES``, that is when it no longer holds a
        person's rows (tests/test_architecture.py refuses a user-keyed table outside the one, ``TheGuard`` a table outside
        the other), which takes a schema change: dropping the table takes its guards with it, and SQLite refuses to drop
        the owning column while a guard reads it, so that release drops the guard by name, knowing the table. Until then
        the guard it leaves keeps the rule every release before it kept, and its sweep does not read the table."""
        store = Store(self.path, KEY)
        bob, _ = seed(store, "bob")
        ada, _ = seed(store, "ada")
        orphan(store, ada)
        store.close()
        with mock.patch("inkcast.libs.db.erasure.OWNERS", without("authz_tuples")), Traced() as traced:
            later = Store(self.path, KEY)
            self.addCleanup(later.close)
            self.assertEqual([s for s in traced.statements if s.startswith(("CREATE TRIGGER", "DROP TRIGGER"))], [],
                             "nothing of its own to add, and nothing it knows to drop")
            self.assertEqual({table for table in self.guards().values() if table == "authz_tuples"}, {"authz_tuples"})
            self.assertEqual(later._exec("INSERT INTO authz_tuples VALUES (?, 'owner', 'organization:x', 'organization', 0)",
                                         f"user:{ada}"), 0, "the guard it left still refuses a person with no users row")
            self.assertEqual(later._exec("INSERT INTO authz_tuples VALUES (?, 'owner', 'organization:x', 'organization', 0)",
                                         f"user:{bob}"), 1, "and lets a living person's row in")
            statements: list[str] = []
            later._conn.set_trace_callback(statements.append)
            swept = later.sweep_orphans()
            later._conn.set_trace_callback(None)
        self.assertNotIn("authz_tuples", swept)
        self.assertEqual([s for s in statements if "FROM authz_tuples" in s], [], "not its table to sweep")
        self.assertEqual(rows(later, "authz_tuples", TUPLES, ada), 1, "left as it was, for a release that knows the table")


class TheSweepAcrossReleases(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = str(self.dir / "inkcast.db")
        self.store = Store(self.path, KEY)
        self.addCleanup(self.store.close)

    def reads(self, call) -> tuple[object, list[str]]:
        """What ``call()`` returned, and the tables its sweep read, each once, in order (a table is read a batch at a
        time and once more to find nothing left)."""
        statements: list[str] = []
        self.store._conn.set_trace_callback(statements.append)
        try:
            result = call()
        finally:
            self.store._conn.set_trace_callback(None)
        return result, list(dict.fromkeys(s.removeprefix("SELECT rowid FROM ").split(" WHERE ")[0]
                                          for s in statements if s.startswith("SELECT rowid FROM ")))

    def test_a_table_added_to_owners_later_is_swept_once_on_a_database_already_marked(self):
        bob, _ = seed(self.store, "bob")
        ada, _ = seed(self.store, "ada")
        orphan(self.store, ada)
        before = rows_of(self.store, bob)
        with mock.patch("inkcast.libs.db.erasure.OWNERS", without("authz_tuples")):
            swept = self.store.sweep_orphans()                       # the release before this one swept, and marked
        self.assertNotIn("authz_tuples", swept)
        self.assertEqual(rows(self.store, "authz_tuples", TUPLES, ada), 1, "the table that release did not know")
        # This release, which added the table: it is swept once, and the others are not read again.
        swept, read = self.reads(self.store.sweep_orphans)
        self.assertEqual(swept, {"authz_tuples": 1})
        self.assertEqual(read, ["authz_tuples"], "the one table the marker lacked, and no other")
        self.assertEqual(rows_of(self.store, ada), ZEROS)
        self.assertEqual(rows_of(self.store, bob), before)
        self.assertEqual(json.loads(self.store.get_sync_state(ORPHANS_SWEPT)),
                         sorted(dict.fromkeys(owner[0] for owner in OWNERS)), "the marker lists every table swept")
        # Done: the marker is read, and nothing else.
        statements: list[str] = []
        self.store._conn.set_trace_callback(statements.append)
        self.assertEqual(self.store.sweep_orphans(), {})
        self.store._conn.set_trace_callback(None)
        self.assertEqual(len(statements), 1, statements)
        self.assertNotIn("NOT EXISTS", statements[0])

    def test_a_marker_from_before_the_list_means_the_tables_of_the_release_that_wrote_it(self):
        """``"1"``, as every release from PR #208 to the list wrote it: those tables are not read again (their orphans
        are left where they are, as that marker said), and a table added to ``OWNERS`` since is swept once."""
        bob, _ = seed(self.store, "bob")
        ada, _ = seed(self.store, "ada")
        orphan(self.store, ada)
        orphans = rows_of(self.store, ada)
        self.store._exec("INSERT INTO sync_state VALUES (?, '1', ?)", ORPHANS_SWEPT, time.time())
        self.store._exec("CREATE TABLE late_table (user_id TEXT NOT NULL)")      # a table a later release adds
        self.store._exec("INSERT INTO late_table VALUES (?), (?)", ada, bob)
        late = OWNERS + (("late_table", "user_id", "{row}.user_id", None),)
        with mock.patch("inkcast.libs.db.erasure.OWNERS", late):
            swept, read = self.reads(self.store.sweep_orphans)
        self.assertEqual(swept, {"late_table": 1})
        self.assertEqual(read, ["late_table"])
        self.assertEqual([r[0] for r in self.store._all("SELECT user_id FROM late_table")], [bob])
        self.assertEqual(rows_of(self.store, ada), orphans, "what the old marker covered is not read again")
        self.assertEqual(json.loads(self.store.get_sync_state(ORPHANS_SWEPT)),
                         sorted([*dict.fromkeys(owner[0] for owner in OWNERS), "late_table"]))
        # A release from before the list reads any marker as done: the row is there.
        self.assertIsNotNone(self.store._one("SELECT 1 FROM sync_state WHERE name = ?", ORPHANS_SWEPT))

    def test_a_backup_from_before_the_sweep_is_swept_again_after_a_restore(self):
        bob, _ = seed(self.store, "bob")
        ada, _ = seed(self.store, "ada")
        orphan(self.store, ada)
        orphans = rows_of(self.store, ada)
        backups = self.dir / "backups"
        take_backup(pathlib.Path(self.path), backups, version="test")
        self.assertEqual(self.store.sweep_orphans()["posts"], 1)
        self.assertEqual(rows_of(self.store, ada), ZEROS)
        self.store.close()
        restore_backup(backups / sorted(p.name for p in backups.glob("*.db"))[-1], pathlib.Path(self.path),
                       now=time.time() + 3600)
        self.store = Store(self.path, KEY)
        self.addCleanup(self.store.close)
        self.assertIsNone(self.store.get_sync_state(ORPHANS_SWEPT), "the marker lives in the database: the backup has none")
        self.assertEqual(rows_of(self.store, ada), orphans, "the backup predates the sweep: her rows are back")
        self.assertEqual(self.store.sweep_orphans()["posts"], 1)
        self.assertEqual(rows_of(self.store, ada), ZEROS)
        self.assertEqual(len(self.store.user_erasures()), 1, "no second audit row, then or now")


class AnotherProcessSweeps(TwoConnections):
    def test_a_batch_another_process_deleted_first_does_not_end_the_tables_sweep(self):
        """Four orphan posts, batches of three: between the sweep's read of its first batch and its delete, another
        process removes every orphan post there is and a fifth person's post becomes an orphan. The delete finds none
        of its batch; the next read finds the fifth, and the table is clean when the database is marked."""
        people = [seed(self.api, f"p{i}")[0] for i in range(4)]
        for uid in people:
            orphan(self.api, uid)
        condition = orphan_condition("posts", "posts")
        real_tx, raced = Store._tx, []

        def other_process_first(store):
            if not raced:
                raced.append(True)
                self.identity._exec(f"DELETE FROM posts WHERE {condition}")
                fifth, _ = seed(self.identity, "p4")
                orphan(self.identity, fifth)
                people.append(fifth)
            return real_tx(store)
        with mock.patch.object(Store, "ORPHAN_BATCH", 3), mock.patch.object(Store, "_tx", other_process_first):
            swept = self.api.sweep_orphans()
        self.assertEqual(raced, [True])
        self.assertEqual(swept.get("posts", 0), 1, "the fifth; its first batch of three was gone before its delete")
        self.assertEqual(sum(rows_of(self.api, uid)["posts"] for uid in people), 0, "the table's sweep went on to the rest")
        self.assertEqual(sum(sum(rows_of(self.api, uid).values()) for uid in people), 0, "and so did the others")
        self.assertIsNotNone(self.api.get_sync_state(ORPHANS_SWEPT))


class AllOrNothing(unittest.TestCase):
    """A tuple for a ``user:`` subject who has no ``users`` row, in a write with others: the guard ignores that row where
    it is written, so the repository refuses the whole write, as OpenFGA's is applied whole or not at all."""

    def setUp(self):
        self.store = Store(":memory:", KEY)
        self.addCleanup(self.store.close)
        self.bob = self.store.upsert_user("oidc:x#bob", "bob", None, None)["id"]
        self.ada = self.store.upsert_user("oidc:x#ada", "ada", None, None)["id"]
        self.assertEqual(self.store.erase_user(self.ada, as_read=self.store.get_user(self.ada))["users"], 1)

    def test_a_write_with_a_tuple_for_a_person_who_has_no_users_row_is_refused_whole(self):
        grant = (f"user:{self.bob}", "owner", "organization:acme")
        with self.assertRaises(ValueError) as caught:
            self.store.authz_write([grant, (f"user:{self.ada}", "viewer", "workspace:w")])
        from inkcast.libs.db import NoSuchUser
        self.assertIsInstance(caught.exception, NoSuchUser)
        self.assertNotIn(self.ada, str(caught.exception), "no person's id in the message")
        self.assertEqual(self.store.authz_read(), [], "nothing of it landed: not bob's tuple either")
        # The deletes of the same write are undone with it.
        self.store.authz_write([grant])
        with self.assertRaises(NoSuchUser):
            self.store.authz_write([(f"user:{self.ada}", "owner", "organization:acme")], deletes=[grant])
        self.assertEqual(self.store.authz_read(), [grant], "the delete was rolled back with the write")
        # A userset is nobody's row; a living person's tuple lands; a tuple that exists is still a conflict.
        self.store.authz_write([("organization:acme#member", "viewer", "workspace:w"), (f"user:{self.bob}", "viewer", "workspace:w")])
        self.assertEqual(len(self.store.authz_read()), 3)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.authz_write([grant])

    def test_the_local_engine_answers_an_invalid_request_and_leaves_the_tuples_as_they_were(self):
        engine = LocalEngine(self.store)
        grant = Tuple(f"user:{self.bob}", "owner", "organization:acme")
        asyncio.run(engine.write([grant]))
        with self.assertRaisesRegex(InvalidRequest, "no account"):
            asyncio.run(engine.write([Tuple(f"user:{self.ada}", "viewer", "workspace:w"),
                                      Tuple(f"user:{self.bob}", "viewer", "workspace:w")], deletes=[grant]))
        self.assertEqual(asyncio.run(engine.read()), [grant])


if __name__ == "__main__":
    unittest.main()
