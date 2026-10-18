# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The database connection every repository shares: one SQLite file in WAL mode.

Each domain repository (identity, vault, posts, ledger) contributes its own ``SCHEMA`` and is
owned by exactly one service; see docs/epics/E1-foundations/design.md. Postgres arrives in E3
behind this same class.
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

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self._conn.executescript("PRAGMA journal_mode = WAL;\n" + "\n".join(self.SCHEMAS))
        for table, column, declaration in self.COLUMNS:
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

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
