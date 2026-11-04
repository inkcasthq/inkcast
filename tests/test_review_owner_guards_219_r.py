# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The adversarial review of PR #426 (issue #219).

* Two releases whose guard on a table both know differs (a rollout in split mode: a pod of the new image starting while
  one of the previous image restarts) each read the schema, then create their own guard and drop the other's. When the
  second opener reads the schema after the first created its guard and before the first dropped the other's, each drops
  the other's and the table is left with none, until the next open: ``install_owner_guards`` read and changed the
  schema in autocommit statements, so nothing ordered two opens. Now the change is made under the write lock, after
  reading the schema again there (as the columns are, issue #176): the opens are ordered, and the last one's guard is
  in place.
* A marker that is neither ``"1"`` nor a JSON list of table names (a JSON object, a string) was read as a list of
  whatever iterating it gave, so ``{"posts": 1}`` counted ``posts`` as swept and its orphans stayed; one that iterates
  to something else (``[1]``) failed only after every table had been read and swept, at every housekeeping. Now any
  other marker is refused before a table is read, naming the marker and what to do.
* The rows removed per table are for the tables that had any: a table whose orphans another process removed first is
  left out, not reported with 0 (``sweep_orphans`` adds only a batch that removed rows; a mutant that adds every batch
  passed the PR's tests).
"""

from __future__ import annotations

import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from inkcast.libs.db import Store
from inkcast.libs.db.erasure import ORPHANS_SWEPT, OWNERS, ErasureRepo, orphan_condition
from test_erasure_orphans import KEY, PAYLOAD, TwoConnections, orphan, rows_of, seed


class RacingOpener:
    """A connection that runs ``hook`` once, just before its first ``DROP TRIGGER``: another process's open landing
    between this one's reads and its drops."""

    def __init__(self, conn: sqlite3.Connection, hook):
        self.conn, self.hook, self.fired = conn, hook, False

    def execute(self, sql: str, *args):
        if sql.startswith("DROP TRIGGER") and not self.fired:
            self.fired = True
            self.hook()
        return self.conn.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self.conn, name)


class TwoReleasesOpenAtOnce(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = str(self.dir / "inkcast.db")

    def posts_guards(self) -> list[str]:
        conn = sqlite3.connect(self.path)
        try:
            return [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'"
                                               " AND tbl_name = 'posts' AND name LIKE 'owner_guard_posts_insert_%'")]
        finally:
            conn.close()

    def test_an_older_and_a_newer_release_opening_at_once_leave_a_guard_on_a_table_both_know(self):
        newer_owners = OWNERS
        older_owners = tuple(("posts", column, person, "{row}.status != 'draft'") if table == "posts" else
                             (table, column, person, where) for table, column, person, where in OWNERS)
        newer = Store(self.path, KEY)                                   # the new image's pod: its guards in place
        self.addCleanup(newer.close)
        with mock.patch.object(Store, "install_owner_guards", lambda store: 0):
            older = Store(self.path, KEY)                               # the previous image's pod, about to install
        self.addCleanup(older.close)

        def the_newer_pod_opens_again():
            with mock.patch("inkcast.libs.db.erasure.OWNERS", newer_owners):
                newer.install_owner_guards()
        older._conn = RacingOpener(older._conn, the_newer_pod_opens_again)
        with mock.patch("inkcast.libs.db.erasure.OWNERS", older_owners):
            ErasureRepo.install_owner_guards(older)
        self.assertTrue(older._conn.fired, "the newer pod's open ran between the older pod's read and its drops")
        self.assertEqual(len(self.posts_guards()), 1, "one release's insert guard on posts, never none")
        newer.create_post(group_id="g", user_id="usr_nobody", platform="fake", title="T", status="scheduled",
                          run_at=time.time(), payload=PAYLOAD)
        self.assertEqual(newer._one("SELECT COUNT(*) AS n FROM posts WHERE user_id = 'usr_nobody'")["n"], 0,
                         "a post for a person with no users row is still refused")


class AMarkerOfAnotherForm(unittest.TestCase):
    def test_is_refused_before_a_table_is_read_and_says_what_to_do(self):
        for marker in ('{"posts": 1}', '"posts"', "[1]", "", "0", "null"):
            with self.subTest(marker=marker):
                store = Store(":memory:", KEY)
                self.addCleanup(store.close)
                ada, _ = seed(store, "ada")
                orphan(store, ada)
                orphans = rows_of(store, ada)
                store._exec("INSERT INTO sync_state VALUES (?, ?, ?)", ORPHANS_SWEPT, marker, time.time())
                statements: list[str] = []
                store._conn.set_trace_callback(statements.append)
                with self.assertRaisesRegex(ValueError, f"{ORPHANS_SWEPT} is neither '1' nor a JSON list of table names:"
                                                        " delete the row"):
                    store.sweep_orphans()
                store._conn.set_trace_callback(None)
                self.assertEqual([s for s in statements if s.startswith(("SELECT rowid", "DELETE", "INSERT"))], [],
                                 "no table read, nothing deleted, the marker left for the operator")
                self.assertEqual(rows_of(store, ada), orphans)
                self.assertEqual(store.get_sync_state(ORPHANS_SWEPT), marker)

    def test_a_list_and_the_old_marker_are_read_as_before(self):
        store = Store(":memory:", KEY)
        self.addCleanup(store.close)
        ada, _ = seed(store, "ada")
        orphan(store, ada)
        store._exec("INSERT INTO sync_state VALUES (?, '[\"posts\"]', ?)", ORPHANS_SWEPT, time.time())
        swept = store.sweep_orphans()
        self.assertNotIn("posts", swept)
        self.assertEqual(rows_of(store, ada)["posts"], 1, "posts is listed: not read again")
        self.assertEqual(store.sweep_orphans(), {})


class AnotherProcessTookEveryOrphan(TwoConnections):
    def test_the_table_is_left_out_of_the_rows_removed(self):
        ada, _ = seed(self.api, "ada")
        orphan(self.api, ada)
        condition = orphan_condition("posts", "posts")
        real_tx, raced = Store._tx, []

        def other_process_first(store):
            if not raced:                           # the sweep's first delete is posts'
                raced.append(self.identity._exec(f"DELETE FROM posts WHERE {condition}"))
            return real_tx(store)
        with mock.patch.object(Store, "_tx", other_process_first):
            swept = self.api.sweep_orphans()
        self.assertEqual(raced, [1], "the other process removed her post first")
        self.assertNotIn("posts", swept, "no rows removed there by this sweep")
        self.assertEqual(sum(rows_of(self.api, ada).values()), 0, "and every other table of hers swept")
        self.assertTrue(all(swept.values()), swept)


if __name__ == "__main__":
    unittest.main()
