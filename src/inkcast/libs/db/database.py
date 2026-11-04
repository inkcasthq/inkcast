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

That zeroing reaches the files on disk only through a checkpoint (issue #404). In WAL mode a
transaction's pages go to the write-ahead log, so after an erasure the database file still holds
the person's pages until a checkpoint copies the zeroed ones over them, and the log file keeps
every frame it ever held, the person's included, until it is truncated: a checkpoint lets the
writer start over at the beginning of the log, it does not cut the file (sqlite.org/wal.html §6),
and the automatic one runs only once the log holds 1000 pages. ``checkpoint_wal`` runs
``PRAGMA wal_checkpoint(TRUNCATE)``, which copies every frame and cuts the log to zero bytes
(sqlite.org/pragma.html#pragma_wal_checkpoint); every erasure runs it, and housekeeping runs it
again for the cases a reader on an older snapshot held up.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

BUSY_TIMEOUT_MS = 30_000        # how long a statement waits for the write lock
# How long a checkpoint waits for readers on an older snapshot before it gives up (busy): a statement's snapshot lasts
# well under a millisecond, a backup's copy seconds, and a checkpoint that cannot complete is run again by housekeeping,
# so it waits for the first and never for the second. Measured: 256.7 to 264.2 ms to give up against a held snapshot,
# in attempts WAL_CHECKPOINT_RETRY_MS apart (#470); 0.7 to 13 ms to copy 0.85 to 27 MB of frames once the reader is
# gone (PR for #404).
WAL_CHECKPOINT_WAIT_MS = 250
# The pause between two attempts of that checkpoint: it waits here, outside SQLite, rather than in SQLite's busy
# handler, which holds the database's write lock meanwhile (issue #470).
WAL_CHECKPOINT_RETRY_MS = 10


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
        self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
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

    def checkpoint_wal(self) -> tuple[int, int, int]:
        """``PRAGMA wal_checkpoint(TRUNCATE)``: every committed page copied into the database file, the file synced,
        and the write-ahead log cut to zero bytes, so nothing a transaction overwrote (an erased person's rows, #404)
        stays in either file. Returns the pragma's row, ``(busy, frames, checkpointed)``: ``busy`` is 1 when a reader on
        an older snapshot kept the log in use for longer than WAL_CHECKPOINT_WAIT_MS, and the log then keeps its frames
        (``frames`` of them) until a checkpoint completes, which housekeeping runs every HOUSEKEEPING_EVERY. Each attempt
        gives up at once (no busy handler: it would hold the database's write lock while it waits, and a write from
        any connection would wait with it; sqlite.org/c3ref/wal_checkpoint_v2.html), under this connection's lock for
        the attempt alone, so this process reads and writes between attempts (issue #470). The wait is the caller's
        thread sleeping: async code runs it in a worker thread. Outside a transaction only: the callers run it after
        their commit."""
        deadline = time.monotonic() + WAL_CHECKPOINT_WAIT_MS / 1000
        while True:
            with self._lock:
                self._conn.execute("PRAGMA busy_timeout = 0")
                try:
                    busy, frames, checkpointed = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                finally:
                    self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if not busy or time.monotonic() >= deadline:
                return int(busy), int(frames), int(checkpointed)
            time.sleep(WAL_CHECKPOINT_RETRY_MS / 1000)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
