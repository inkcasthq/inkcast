# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Erasure (E3.F3.3.S5, the user half): everything Inkcast holds about one person goes in one transaction.

``USER_TABLES`` names every table that holds a person's rows and the condition that finds them. It is the
single list ``erase_user`` deletes from, and ``tests/test_architecture.py`` compares it with the live schema:
a new table with a ``user_id`` column (or another user-keyed column) fails the build until it is listed here.

``user_erasures`` is the audit record: a salted hash of the user id (HMAC-SHA256 under a key derived from
INKCAST_SECRET_KEY, so the id can be checked against the record but not read from it), the time, and the
counts. Nothing else about the person survives in the database. Copies in backups do, until they age out
(INKCAST_BACKUP_KEEP daily copies), and a restore brings them back: ``reapply_erasures`` (issue #94), run by every
process when it opens the store, erases again anyone whose hash is in ``user_erasures`` but who has rows. The
restore carries the audit rows of the database it replaces into the restored one (libs/db/backup.py), which is
what makes that possible: a backup taken before the erasure holds the rows and not the record.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time

from .database import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_erasures (
  user_hash TEXT NOT NULL, erased_at REAL NOT NULL, counts TEXT NOT NULL
);
"""

# (table, the condition that picks the person's rows). Deleted together in one transaction under the write lock
# a scheduler claim (``claim_due_posts``) also takes, so a claim runs either before every row or after none is left.
# Not here on purpose: ``deletion_requests`` (the pending "delete my account" record) outlives the erasure so the
# visit back from Keycloak can still be answered; it holds the user id and hashes only, expires, and is purged by
# housekeeping and deleted once answered (tests/test_architecture.py lists it with that reason).
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
    ("sync_breaker_people", "user_id = :user_id"),      # a pass the Keycloak sync's breaker holds for an operator (#105)
    ("kpi_rollups", "scope_kind = 'user' AND scope_id = :user_id"),
    ("authz_tuples", "subject = 'user:' || :user_id"),
    ("users", "id = :user_id"),
)


def user_hash(salt: bytes, user_id: str) -> str:
    """How the audit record refers to a person: HMAC-SHA256 of the user id under the erasure key."""
    return hmac.new(salt, user_id.encode(), hashlib.sha256).hexdigest()


class ErasureRepo(Database):
    _erasure_salt: bytes

    @staticmethod
    def _delete_person(c: sqlite3.Connection, user_id: str) -> dict[str, int]:
        """The person's rows out of every table in ``USER_TABLES``, inside the caller's transaction; the counts."""
        return {table: int(c.execute(f"DELETE FROM {table} WHERE {condition}", {"user_id": user_id}).rowcount)
                for table, condition in USER_TABLES}

    def erase_user(self, user_id: str) -> dict[str, int]:
        """Delete the person's rows from every table in ``USER_TABLES`` in one transaction and record the erasure.

        The counts per table, only ever ints; all zeros (and no audit record) for a person who is unknown or was
        already erased, so it is safe to run twice or from two places."""
        now = time.time()
        with self._tx() as c:
            counts = self._delete_person(c, user_id)
            if any(counts.values()):
                c.execute("INSERT INTO user_erasures VALUES (?, ?, ?)",
                          (user_hash(self._erasure_salt, user_id), now, json.dumps(counts, sort_keys=True)))
        return counts

    def reapply_erasures(self) -> dict[str, dict[str, int]]:
        """Erase again every person whose hash is in ``user_erasures`` but who has rows: a database restored from a
        backup taken before their erasure (issue #94). Returns ``{user_id: counts}`` for those erased, in one
        transaction each; no second audit row (the record of the erasure is the one already there), so the caller
        logs it and counts nothing as a new deletion. Cheap when there is nothing to do: with no audit rows nothing
        is hashed; otherwise one HMAC per user and no write."""
        hashes = {r["user_hash"] for r in self._all("SELECT user_hash FROM user_erasures")}
        if not hashes:
            return {}
        ids = [r["id"] for r in self._all("SELECT id FROM users ORDER BY id")]
        erased: dict[str, dict[str, int]] = {}
        for user_id in ids:
            if user_hash(self._erasure_salt, user_id) not in hashes:
                continue
            with self._tx() as c:
                counts = self._delete_person(c, user_id)
            if any(counts.values()):
                erased[user_id] = counts
        return erased

    def user_erasures(self) -> list[dict[str, object]]:
        """The audit records, oldest first: ``user_hash``, ``erased_at`` and the ``counts`` per table."""
        rows = self._all("SELECT user_hash, erased_at, counts FROM user_erasures ORDER BY erased_at, rowid")
        return [{"user_hash": r["user_hash"], "erased_at": float(r["erased_at"]), "counts": json.loads(r["counts"])}
                for r in rows]


__all__ = ["ErasureRepo", "SCHEMA", "USER_TABLES", "user_hash"]
