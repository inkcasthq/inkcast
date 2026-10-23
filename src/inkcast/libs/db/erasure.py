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
what makes that possible: a backup taken before the erasure holds the rows and not the record. Each person's hash
is stored with their row (``users.erasure_hash``, indexed; libs/db/identity.py), so finding them costs the records,
not a hash of every user in every process (issue #113); ``backfill_erasure_hashes`` fills it once for rows from before
it. The record of an erasure carries the hash stored with the person's row, so the two match whatever key was current
when each was written: a rotation of INKCAST_SECRET_KEY doesn't stop a restored row from being re-erased (#112).

Whoever erases decided on a reading of the person (a 404 from Keycloak, a grace period over) that another replica
may have overtaken by the time the transaction starts, so ``erase_user`` takes the row as the caller read it and
re-checks it inside the transaction (issue #102): a person who signed in meanwhile keeps every row.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from typing import Any, Mapping

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
# What ``erase_user`` re-checks about the person's row (issue #102): their standing, as the caller read it when it
# decided. A sign-in changes last_login_at (and clears the other three); a reconcile that found them present clears
# deleted_at; a pass that cut them off sets disabled_at; a hold or its release changes held_at.
STANDING = ("last_login_at", "deleted_at", "disabled_at", "held_at")


def user_hash(salt: bytes, user_id: str) -> str:
    """How the audit record refers to a person: HMAC-SHA256 of the user id under the erasure key."""
    return hmac.new(salt, user_id.encode(), hashlib.sha256).hexdigest()


class ErasureRepo(Database):
    _erasure_salt: bytes
    ERASURE_HASH_BATCH = 10_000     # rows the backfill reads at a time: a million users in bounded memory

    @staticmethod
    def _delete_person(c: sqlite3.Connection, user_id: str) -> dict[str, int]:
        """The person's rows out of every table in ``USER_TABLES``, inside the caller's transaction; the counts."""
        return {table: int(c.execute(f"DELETE FROM {table} WHERE {condition}", {"user_id": user_id}).rowcount)
                for table, condition in USER_TABLES}

    @staticmethod
    def _as_read(c: sqlite3.Connection, user_id: str, as_read: Mapping[str, Any]) -> bool:
        """Whether the person's row is still there, standing as the caller read it, inside the caller's transaction."""
        row = c.execute("SELECT last_login_at, deleted_at, disabled_at, held_at FROM users WHERE id = ?", (user_id,)).fetchone()
        return row is not None and all(row[column] == as_read[column] for column in STANDING)

    def erase_user(self, user_id: str, as_read: Mapping[str, Any] | None = None) -> dict[str, int]:
        """Delete the person's rows from every table in ``USER_TABLES`` in one transaction and record the erasure.

        ``as_read`` is the person's ``users`` row as the caller read it when it decided to erase (issue #102): the
        row is read again inside the transaction, under the write lock, and nothing is erased unless it is still
        there and standing as read (``STANDING``: the same ``last_login_at``, ``deleted_at``, ``disabled_at`` and
        ``held_at``). A sign-in handled by another replica in between is the one proof Inkcast has that Keycloak has
        the person; a pass elsewhere that found them present, cut them off or held them has newer word about them
        than the caller's; either way the caller's next pass judges them afresh. Without it the person is erased as
        they stand (``reapply_erasures``, and a caller that read nothing).

        The counts per table, only ever ints; all zeros (and no audit record) for a person who is unknown, was
        already erased, or is not as read, so it is safe to run twice or from two places."""
        now = time.time()
        with self._tx() as c:
            if as_read is not None and not self._as_read(c, user_id, as_read):
                return {table: 0 for table, _ in USER_TABLES}
            # The record carries the hash stored with the person (users.erasure_hash), so a restored row matches it
            # whatever key was current when each was written (issue #113), and the hash under the key of the day when
            # they differ: a backup from before the column is filled under the key current at the restore.
            row = c.execute("SELECT erasure_hash FROM users WHERE id = ?", (user_id,)).fetchone()
            hashes = dict.fromkeys(h for h in (row["erasure_hash"] if row is not None else None,
                                               user_hash(self._erasure_salt, user_id)) if h)
            counts = self._delete_person(c, user_id)
            if any(counts.values()):
                c.executemany("INSERT INTO user_erasures VALUES (?, ?, ?)",
                              [(hashed, now, json.dumps(counts, sort_keys=True)) for hashed in hashes])
        return counts

    def backfill_erasure_hashes(self) -> int:
        """Fill ``users.erasure_hash`` for the rows that have none: those from before the column (issue #113), and
        any a process from before it wrote during a rolling update. ``ERASURE_HASH_BATCH`` rows at a time, each batch
        its own transaction, so a million rows take bounded memory and a start cut short leaves the rest to the next
        one; the rows are taken as the index lists them (an order would sort every row left, in every batch). Run at
        every open (``Store``); once filled it costs one indexed read and no lock. How many were filled."""
        filled = 0
        while self._one("SELECT 1 FROM users WHERE erasure_hash IS NULL LIMIT 1") is not None:
            with self._tx() as c:
                ids = [r["id"] for r in c.execute("SELECT id FROM users WHERE erasure_hash IS NULL LIMIT ?",
                                                  (self.ERASURE_HASH_BATCH,))]
                c.executemany("UPDATE users SET erasure_hash = ? WHERE id = ?",
                              [(user_hash(self._erasure_salt, user_id), user_id) for user_id in ids])
            filled += len(ids)
        return filled

    def reapply_erasures(self) -> dict[str, dict[str, int]]:
        """Erase again every person whose hash is in ``user_erasures`` but who has rows: a database restored from a
        backup taken before their erasure (issue #94). Returns ``{user_id: counts}`` for those erased, in one
        transaction each; no second audit row (the record of the erasure is the one already there), so the caller
        logs it and counts nothing as a new deletion. Nothing is hashed: ``users`` is joined against the records on
        the hash stored with each row, through its index, so the cost is the records and the matches, not the users
        (issue #113); with no record, one read and no write."""
        ids = [r["id"] for r in self._all("SELECT id FROM users WHERE erasure_hash IN (SELECT user_hash FROM user_erasures)"
                                          " ORDER BY id")]
        erased: dict[str, dict[str, int]] = {}
        for user_id in ids:
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


__all__ = ["ErasureRepo", "SCHEMA", "STANDING", "USER_TABLES", "user_hash"]
