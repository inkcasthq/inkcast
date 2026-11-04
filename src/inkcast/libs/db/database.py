# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The database connection every repository shares: one SQLite file in WAL mode.

Each domain repository (identity, vault, posts, ledger) contributes its own ``SCHEMA`` and is
owned by exactly one service; see docs/epics/E1-foundations/design.md. Postgres arrives in E3
behind this same class.

Deleted content is overwritten with zeros (``PRAGMA secure_delete``, a per-connection setting, so
every connection sets it): an erased person's rows leave no bytes in the file's free space, and a
backup taken after the erasure carries none (issue #112). The image's SQLite is built with it on
(Debian's ``SQLITE_SECURE_DELETE``); setting it pins that whatever the build, and a benchmark on
a 41 MB database put the cost at +7 % on an erasure (1.0 to 1.1 ms) and within 2 % elsewhere.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator


class Database:
    SCHEMAS: tuple[str, ...] = ()
    # Columns added after a table first shipped: (table, column, declaration). CREATE TABLE IF NOT EXISTS never
    # adds a column to an existing database, so each is added here once, when it is missing (SQLite until E3).
    COLUMNS: tuple[tuple[str, str, str], ...] = ()
    # Indexes on columns from COLUMNS, each a CREATE INDEX IF NOT EXISTS: created after the columns, since the schemas
    # run before them and an index on a column the table doesn't have yet fails.
    INDEXES: tuple[str, ...] = ()

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self._conn.execute("PRAGMA secure_delete = ON")       # deleted content is zeroed, whatever the build (#112)
        self._conn.executescript("PRAGMA journal_mode = WAL;\n" + "\n".join(self.SCHEMAS))
        # Several processes open the same file at once (split mode's pods on a new image), and each would add the
        # column it found missing: the second ALTER TABLE fails with "duplicate column name" (issue #176). So the
        # columns are added under the write lock, after reading the tables again there, where the process that comes
        # second finds nothing left to add. A database with every column costs the reads and takes no lock.
        if self._missing_columns():
            with self._tx() as c:
                for table, column, declaration in self._missing_columns():
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        for statement in self.INDEXES:
            self._conn.execute(statement)

    def _missing_columns(self) -> list[tuple[str, str, str]]:
        """The entries of ``COLUMNS`` the database doesn't have, as it stands when this reads it."""
        missing = []
        for table, column, declaration in self.COLUMNS:
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                missing.append((table, column, declaration))
        return missing

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _exec(self, sql: str, *args: Any) -> int:
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    def ping(self) -> bool:
        self._one("SELECT 1")
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()
