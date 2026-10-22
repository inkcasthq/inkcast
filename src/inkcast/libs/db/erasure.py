# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Erasure (E3.F3.3.S5, the user half): everything Inkcast holds about one person goes in one transaction.

``USER_TABLES`` names every table that holds a person's rows and the condition that finds them. It is the
single list ``erase_user`` deletes from, and ``tests/test_architecture.py`` compares it with the live schema:
a new table with a ``user_id`` column (or another user-keyed column) fails the build until it is listed here.

``user_erasures`` is the audit record: a salted hash of the user id (HMAC-SHA256 under a key derived from
INKCAST_SECRET_KEY, so the id can be checked against the record but not read from it), the time, and the
counts. Nothing else about the person survives.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from .database import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_erasures (
  user_hash TEXT NOT NULL, erased_at REAL NOT NULL, counts TEXT NOT NULL
);
"""

# (table, the condition that picks the person's rows). Deleted together in one transaction under the write lock
# a scheduler claim (``claim_due_posts``) also takes, so a claim runs either before every row or after none is left.
USER_TABLES: tuple[tuple[str, str], ...] = (
    ("posts", "user_id = :user_id"),
    ("outbox", "user_id = :user_id"),
    ("ledger", "user_id = :user_id"),
    ("idempotency", "user_id = :user_id"),
    ("credentials", "user_id = :user_id"),
    ("connect_tickets", "user_id = :user_id"),
    ("tokens", "user_id = :user_id"),
    ("auth_codes", "user_id = :user_id"),
    ("pending_auth", "user_id = :user_id"),
    ("api_tokens", "user_id = :user_id"),
    ("deletion_requests", "user_id = :user_id"),
    ("kpi_rollups", "scope_kind = 'user' AND scope_id = :user_id"),
    ("authz_tuples", "subject = 'user:' || :user_id"),
    ("users", "id = :user_id"),
)


def user_hash(salt: bytes, user_id: str) -> str:
    """How the audit record refers to a person: HMAC-SHA256 of the user id under the erasure key."""
    return hmac.new(salt, user_id.encode(), hashlib.sha256).hexdigest()


class ErasureRepo(Database):
    _erasure_salt: bytes

    def erase_user(self, user_id: str) -> dict[str, int]:
        """Delete the person's rows from every table in ``USER_TABLES`` in one transaction and record the erasure.

        The counts per table, only ever ints; all zeros (and no audit record) for a person who is unknown or was
        already erased, so it is safe to run twice or from two places."""
        counts: dict[str, int] = {}
        now = time.time()
        with self._tx() as c:
            for table, condition in USER_TABLES:
                counts[table] = int(c.execute(f"DELETE FROM {table} WHERE {condition}", {"user_id": user_id}).rowcount)
            if any(counts.values()):
                c.execute("INSERT INTO user_erasures VALUES (?, ?, ?)",
                          (user_hash(self._erasure_salt, user_id), now, json.dumps(counts, sort_keys=True)))
        return counts

    def user_erasures(self) -> list[dict[str, object]]:
        """The audit records, oldest first: ``user_hash``, ``erased_at`` and the ``counts`` per table."""
        rows = self._all("SELECT user_hash, erased_at, counts FROM user_erasures ORDER BY erased_at, rowid")
        return [{"user_hash": r["user_hash"], "erased_at": float(r["erased_at"]), "counts": json.loads(r["counts"])}
                for r in rows]


__all__ = ["ErasureRepo", "SCHEMA", "USER_TABLES", "user_hash"]
