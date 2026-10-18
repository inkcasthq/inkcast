# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The transactional outbox: domain events written with the row change they describe, read in order
by consumers that each keep their own cursor (owned by whichever service wrote the row; drained by
InsightsService today).

Repositories append events inside their own transaction with ``insert_events(conn, events)``; nothing
else writes this table. ``poll`` returns the next batch after a consumer's cursor; ``advance`` moves the
cursor once the consumer has handled the batch. ``purge_outbox`` drops rows every cursor has passed and
that are older than the retention, so the table never grows without bound.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Iterable

from ..events import DomainEvent
from .database import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, type TEXT NOT NULL, at REAL NOT NULL,
  user_id TEXT, org_id TEXT, workspace_id TEXT, subject TEXT, attrs TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_cursors (consumer TEXT PRIMARY KEY, seq INTEGER NOT NULL, at REAL NOT NULL);
"""


def insert_events(conn: sqlite3.Connection, events: Iterable[DomainEvent] | None) -> None:
    """Append events on an open transaction (the caller's), so they commit or roll back with the change."""
    for event in events or ():
        conn.execute("INSERT INTO outbox (id, type, at, user_id, org_id, workspace_id, subject, attrs)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", event.to_row())


class OutboxRepo(Database):
    def enqueue(self, *events: DomainEvent) -> None:
        """Events on their own (no row change of their own to join). Prefer passing ``events=`` to the repository
        method that changes the row."""
        with self._tx() as c:
            insert_events(c, events)

    def poll(self, consumer: str, limit: int = 200) -> list[DomainEvent]:
        """The next events after ``consumer``'s cursor, oldest first."""
        rows = self._all("SELECT * FROM outbox WHERE seq > ? ORDER BY seq LIMIT ?", self.cursor(consumer), limit)
        return [DomainEvent.from_row(r) for r in rows]

    def cursor(self, consumer: str) -> int:
        row = self._one("SELECT seq FROM outbox_cursors WHERE consumer = ?", consumer)
        return int(row["seq"]) if row else 0

    def advance(self, consumer: str, seq: int) -> None:
        self._exec("INSERT OR REPLACE INTO outbox_cursors VALUES (?, ?, ?)", consumer, seq, time.time())

    def outbox_lag(self) -> dict[str, tuple[int, float]]:
        """Per consumer: (events not yet handled, age in seconds of the oldest of them); {} with no consumers."""
        head = self._one("SELECT MAX(seq) AS seq FROM outbox")
        head_seq = int(head["seq"]) if head and head["seq"] is not None else 0
        out: dict[str, tuple[int, float]] = {}
        for row in self._all("SELECT consumer, seq FROM outbox_cursors"):
            pending = max(0, head_seq - int(row["seq"]))
            oldest = self._one("SELECT MIN(at) AS at FROM outbox WHERE seq > ?", row["seq"]) if pending else None
            age = max(0.0, time.time() - float(oldest["at"])) if oldest and oldest["at"] is not None else 0.0
            out[row["consumer"]] = (pending, age)
        return out

    def purge_outbox(self, keep_seconds: float = 7 * 86400) -> int:
        """Delete events older than ``keep_seconds`` that every consumer has passed."""
        row = self._one("SELECT MIN(seq) AS seq FROM outbox_cursors")
        if not row or row["seq"] is None:
            return 0
        return self._exec("DELETE FROM outbox WHERE seq <= ? AND at < ?", int(row["seq"]), time.time() - keep_seconds)


__all__ = ["OutboxRepo", "SCHEMA", "insert_events"]
