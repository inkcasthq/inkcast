# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #176: two processes adding the same column at once must both come up.

In split mode every service pod opens the one SQLite file at start-up, and a new image's ``COLUMNS`` entry
(libs/db/database.py) is added by whichever pod finds it missing. On main the check and the ``ALTER TABLE`` were two
statements with no lock between them, so two pods starting on the new image could both find the column missing, and
the second ``ALTER TABLE`` failed with "duplicate column name": that pod crashed once and converged on restart.

The race is forced, never waited for: each open runs on a connection whose ``execute`` hands control to the test at
the moment the open has read the table's columns and is about to act on what it read (the first statement after the
schema reads). One test runs the whole of the second open in that window; the other lets both opens read the table
before either acts, with events between two threads. No sleeps, two real connections on one file."""

import pathlib
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from inkcast.libs.db import Store
from inkcast.libs.db.database import Database

KEY = "k" * 40
_CONNECT = sqlite3.connect             # the database module's sqlite3 is this module: a patch of its connect is global


class Hooked(sqlite3.Connection):
    """A connection that calls ``hook(sql)`` once, before the first statement that follows a read of a table's
    columns: the open has decided what to add and is about to act on it."""

    hook = None
    _read = False
    _fired = False

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA table_info"):
            self._read = True
        elif self._read and not self._fired and self.hook is not None:
            self._fired = True
            self.hook(sql)
        return super().execute(sql, *args)


def opening_with(hook):
    """Patch the database module's ``sqlite3.connect`` so the next open runs on a ``Hooked`` connection."""
    def connect(path, **kwargs):
        conn = _CONNECT(path, factory=Hooked, **kwargs)
        conn.hook = hook
        return conn
    return mock.patch("inkcast.libs.db.database.sqlite3.connect", connect)


def before_the_column(path: pathlib.Path) -> None:
    """The database as the deployment had it before #113: every column but ``users.erasure_hash``, with people."""
    store = Store(str(path), KEY)
    for name in ("one", "two", "three"):
        store.upsert_user(f"oidc:x#{name}", name, None, None)
    store.close()
    conn = sqlite3.connect(path)
    conn.executescript("DROP INDEX users_erasure_hash; ALTER TABLE users DROP COLUMN erasure_hash;")
    conn.close()


class TwoPodsAddingTheSameColumn(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"

    def columns(self, table: str) -> list[str]:
        conn = sqlite3.connect(self.db)
        try:
            return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()

    def test_the_whole_of_one_open_between_the_other_s_read_and_its_add(self):
        """Pod B starts, adds the column and comes up while pod A has read the table and not yet acted."""
        before_the_column(self.db)
        acted_on = []

        def pod_b_runs_to_the_end(sql):
            acted_on.append(sql.split("(")[0].strip())
            with opening_with(None):
                Store(str(self.db), KEY).close()
            self.assertIn("erasure_hash", self.columns("users"), "B added the column")
        with opening_with(pod_b_runs_to_the_end):
            store = Store(str(self.db), KEY)                   # on main: "duplicate column name: erasure_hash"
        self.addCleanup(store.close)
        self.assertEqual(len(acted_on), 1, "A acted on its read once")
        self.assertEqual(self.columns("users").count("erasure_hash"), 1)
        self.assertIsNotNone(store.get_user(store.find_user_by_identity("oidc:x#one")["id"]))
        self.assertEqual(store._one("SELECT COUNT(*) AS n FROM users WHERE erasure_hash IS NULL")["n"], 0, "filled")

    def test_both_read_the_table_before_either_acts(self):
        """Pods A and B both find the column missing; A adds it; then B acts on its stale read. Any future column
        does the same, so this one is a plain ``Database`` with a column of its own."""

        class Widgets(Database):
            SCHEMAS = ("CREATE TABLE IF NOT EXISTS widgets (id TEXT PRIMARY KEY);",)
            COLUMNS = (("widgets", "colour", "TEXT"),)
            INDEXES = ("CREATE INDEX IF NOT EXISTS widgets_colour ON widgets(colour)",)

        Widgets(str(self.db)).close()
        conn = sqlite3.connect(self.db)
        conn.executescript("DROP INDEX widgets_colour; ALTER TABLE widgets DROP COLUMN colour;")
        conn.close()
        b_has_read, a_is_done = threading.Event(), threading.Event()
        outcome: dict[str, object] = {}

        def pod_b():
            try:
                with opening_with(lambda sql: (b_has_read.set(), self.assertTrue(a_is_done.wait(60), "A finished"))):
                    Widgets(str(self.db)).close()
                outcome["b"] = "up"
            except BaseException as exc:                      # noqa: BLE001  reported from the main thread
                outcome["b"] = exc

        def pod_a_waits_for_b_to_read(sql):
            thread = threading.Thread(target=pod_b, name="pod-b")
            thread.start()
            outcome["thread"] = thread
            self.assertTrue(b_has_read.wait(60), "B read the table")
        with opening_with(pod_a_waits_for_b_to_read):
            Widgets(str(self.db)).close()
        a_is_done.set()
        outcome["thread"].join(60)
        self.assertEqual(outcome.get("b"), "up", f"B's open: {outcome.get('b')!r}")   # on main: duplicate column name
        self.assertEqual(self.columns("widgets"), ["id", "colour"])
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertIn("widgets_colour", [r[1] for r in conn.execute("PRAGMA index_list(widgets)")])


if __name__ == "__main__":
    unittest.main()
