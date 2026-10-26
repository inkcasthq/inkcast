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
when each was written: a rotation of INKCAST_SECRET_KEY doesn't stop a restored row from being re-erased (#112). A row
restored from before the column is filled under the key of the restore instead, so when the two differ the hash under
the key current at the erasure is kept beside the record (``user_erasure_aliases``, carried by a restore too).

Whoever erases decided on a reading of the person (a 404 from Keycloak, a grace period over) that another replica
may have overtaken by the time the transaction starts, so ``erase_user`` takes the row as the caller read it and
re-checks it inside the transaction (issue #102): a person who signed in meanwhile keeps every row.

A request that authenticated just before the erasure's transaction can commit its rows just after it (a token, a post
with its event, a credential), and nothing above would find them: no ``users`` row, so no hash to match (issue #196).
So every table in ``USER_TABLES`` refuses a row whose person has no ``users`` row where the write happens: a
``BEFORE INSERT`` trigger per table (and one on an update of the owning column), generated from ``OWNERS`` and
installed at every open (``install_owner_guards``), that ignores the row inside the write's own transaction. The
scheduler's claims look at ``users`` too. The rows such writes left in a database from before the guard are removed by
housekeeping (``sweep_orphans``), once per database, with no second audit row: the record of the erasure is the one
already there, as it is for ``reapply_erasures``.
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
CREATE TABLE IF NOT EXISTS user_erasure_aliases (
  user_hash TEXT NOT NULL, alias TEXT NOT NULL
);
"""
# user_erasure_aliases: the other hash an erasure is known by, beside its one audit row (user_hash): the person's id
# under the key current at the erasure, when the hash stored with their row was written under an earlier key. A backup
# from before users.erasure_hash is filled under the key of the restore, which that alias matches (issue #113).

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

# How a row of each table names its person, for the write-time guard and the sweep (issue #196): (table, the column
# that names them, the expression that is their id, and the rows that are a person's at all, or None for every row);
# ``{row}`` stands for the row being written (``NEW`` in a trigger) or the table. Every table in ``USER_TABLES`` but
# ``users`` itself, which tests/test_erasure_orphans.py checks against it.
OWNERS: tuple[tuple[str, str, str, str | None], ...] = tuple(
    (table, "user_id", "{row}.user_id", None)
    for table in ("posts", "outbox", "ledger", "idempotency", "credentials", "connect_tickets", "tokens", "auth_codes",
                  "pending_auth", "api_tokens", "sync_breaker_people")
) + (
    ("kpi_rollups", "scope_id", "{row}.scope_id", "{row}.scope_kind = 'user'"),
    ("authz_tuples", "subject", "substr({row}.subject, 6)", "{row}.subject LIKE 'user:%'"),       # after 'user:'
)
ORPHANS_SWEPT = "erasure.orphans_swept"     # sync_state: the sweep of rows from before the guard is done here
_GUARD = "owner_guard_"                     # what every trigger of the guard is named after


def orphan_condition(table: str, row: str) -> str:
    """The SQL that is true of a row of ``table`` (``row`` names it) whose person has no ``users`` row."""
    _, _, person, where = next(owner for owner in OWNERS if owner[0] == table)
    person = person.format(row=row)
    missing = f"{person} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM users WHERE users.id = {person})"
    return f"{where.format(row=row)} AND {missing}" if where else missing


def owner_guards() -> list[tuple[str, str]]:
    """(name, CREATE TRIGGER) for every table in ``OWNERS``: a ``BEFORE INSERT`` guard and a ``BEFORE UPDATE`` of the
    owning column, each ignoring a row whose person has no ``users`` row (``RAISE(IGNORE)``: the row is skipped, the
    statement and its transaction go on, and ``rowcount`` leaves it out). The name carries a digest of the body, so a
    change to the body is a new trigger and the old one is dropped (``install_owner_guards``)."""
    out = []
    for table, column, _, _ in OWNERS:
        for kind, on in (("insert", "INSERT"), ("update", f"UPDATE OF {column}")):
            body = (f"BEFORE {on} ON {table} FOR EACH ROW\n  WHEN {orphan_condition(table, 'NEW')}\n"
                    f"BEGIN SELECT RAISE(IGNORE); END")
            name = f"{_GUARD}{table}_{kind}_{hashlib.sha256(body.encode()).hexdigest()[:8]}"
            out.append((name, f"CREATE TRIGGER IF NOT EXISTS {name} {body}"))
    return out


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
        already erased, or is not as read, so it is safe to run twice or from two places. The audit record is written
        when the person's own row went: rows found without one (left by a write in the instant of the erasure, in a
        database from before the guard; issue #196) go too, under the record already there."""
        now = time.time()
        with self._tx() as c:
            if as_read is not None and not self._as_read(c, user_id, as_read):
                return {table: 0 for table, _ in USER_TABLES}
            # The record carries the hash stored with the person (users.erasure_hash), so a restored row matches it
            # whatever key was current when each was written (issue #113); today's for a row from before the column.
            # When a rotation made the two differ, the hash under today's key goes beside it as an alias, not a second
            # audit row: a backup from before the column is filled under the key current at the restore.
            row = c.execute("SELECT erasure_hash FROM users WHERE id = ?", (user_id,)).fetchone()
            today = user_hash(self._erasure_salt, user_id)
            hashed = (row["erasure_hash"] if row is not None else None) or today
            counts = self._delete_person(c, user_id)
            if counts["users"]:
                c.execute("INSERT INTO user_erasures VALUES (?, ?, ?)", (hashed, now, json.dumps(counts, sort_keys=True)))
                if today != hashed:
                    c.execute("INSERT INTO user_erasure_aliases VALUES (?, ?)", (hashed, today))
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
        ids = [r["id"] for r in self._all("SELECT id FROM users WHERE erasure_hash IN (SELECT user_hash FROM user_erasures"
                                          " UNION SELECT alias FROM user_erasure_aliases) ORDER BY id")]
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

    # ---- rows written in the instant of an erasure (issue #196): refused at the write, swept from before ----------

    ORPHAN_BATCH = 1000     # rows the sweep deletes per transaction: the write lock is held for a batch, not a table

    def install_owner_guards(self) -> int:
        """The write-time guard (``owner_guards``) in this database: every trigger it lacks is created, and one from an
        earlier shape of the body (the same prefix, another digest) is dropped once its replacement is in place, so
        there is never a moment without one. Run at every open (``Store``); each statement commits on its own, so a
        start cut short leaves the ones done and the next open does the rest, and two processes opening at once each
        find what the other did (``IF NOT EXISTS``, ``IF EXISTS``). A guard lives in the database, so a process from
        before it is bound as soon as one process installs it. Once in place it costs one read of the schema and no
        write. How many triggers were created or dropped."""
        wanted = dict(owner_guards())
        changed = 0
        with self._lock:
            have = {r["name"] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND substr(name, 1, ?) = ?", (len(_GUARD), _GUARD))}
            for name, statement in wanted.items():
                if name not in have:
                    self._conn.execute(statement)
                    changed += 1
            for name in sorted(have - set(wanted)):
                self._conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                changed += 1
        return changed

    def sweep_orphans(self) -> dict[str, int]:
        """Remove the rows of people who have no ``users`` row from every table in ``OWNERS``: what a request that
        authenticated in the instant of an erasure wrote into a database from before the guard (issue #196). Once per
        database (``sync_state`` remembers; a restore of a backup from before the sweep is swept again, and clearing
        the row runs it once more), in the one process that runs housekeeping, never at start-up: it reads every
        user-keyed table. ``ORPHAN_BATCH`` rows at a time, found without the lock and deleted under it with the
        condition checked again there, each batch its own transaction, so the lock is held for a batch and a run cut
        short leaves the rest to the next. No audit row: the record of each erasure is the one already there. The rows
        removed per table, for the tables that had any; {} once done."""
        if self._one("SELECT 1 FROM sync_state WHERE name = ?", ORPHANS_SWEPT) is not None:
            return {}
        swept: dict[str, int] = {}
        for table, _, _, _ in OWNERS:
            condition = orphan_condition(table, table)
            while True:
                ids = [r[0] for r in self._all(f"SELECT rowid FROM {table} WHERE {condition} LIMIT ?", self.ORPHAN_BATCH)]
                if not ids:
                    break
                with self._tx() as c:
                    n = c.execute(f"DELETE FROM {table} WHERE rowid IN ({', '.join('?' * len(ids))}) AND {condition}",
                                  ids).rowcount
                if n == 0:                      # another sweep got there first: nothing left of this batch
                    break
                swept[table] = swept.get(table, 0) + n
        self._exec("INSERT INTO sync_state VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value,"
                   " updated_at = excluded.updated_at", ORPHANS_SWEPT, "1", time.time())
        return swept


__all__ = ["ErasureRepo", "ORPHANS_SWEPT", "OWNERS", "SCHEMA", "STANDING", "USER_TABLES", "orphan_condition",
           "owner_guards", "user_hash"]
