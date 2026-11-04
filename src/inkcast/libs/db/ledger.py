# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Publish ledger (owned by PublishingApi): which file in which repository became which remote post.

One row per (user, source, path, platform). ``content_hash`` is the hash of what was last sent,
so a re-run with unchanged content is a no-op and a changed file updates the post instead of
creating a duplicate.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from .database import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
  user_id TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL, platform TEXT NOT NULL,
  content_hash TEXT NOT NULL, post_id TEXT, remote_id TEXT, url TEXT, status TEXT NOT NULL,
  message TEXT, synced_at REAL NOT NULL,
  PRIMARY KEY (user_id, source, path, platform)
);
CREATE TABLE IF NOT EXISTS idempotency (
  user_id TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, response TEXT NOT NULL,
  created_at REAL NOT NULL, PRIMARY KEY (user_id, key)
);
"""

# ``replaces``: the post a Git sync's claim replaces (cancelled in the claim's transaction), until the claim has a post
# of its own: a run that fails before then leaves it for the re-run, which keeps that post's creation (#280).
COLUMNS = (("ledger", "replaces", "TEXT"),)

CLAIM_TTL = 600  # a 'syncing' claim older than this was left by a crash and may be taken over

FIELDS = ("content_hash", "post_id", "remote_id", "url", "status", "message", "replaces")


class LedgerRepo(Database):
    def get_entry(self, user_id: str, source: str, path: str, platform: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM ledger WHERE user_id = ? AND source = ? AND path = ? AND platform = ?",
                        user_id, source, path, platform)
        return dict(row) if row else None

    def put_entry(self, user_id: str, source: str, path: str, platform: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            return self.put_entry_in(self._conn, user_id, source, path, platform, **fields)

    @staticmethod
    def put_entry_in(c: sqlite3.Connection, user_id: str, source: str, path: str, platform: str,
                     **fields: Any) -> dict[str, Any]:
        """``put_entry`` on ``c``, inside a transaction another write holds: the entry is written with that write or
        not at all (Git sync's claim, with the cancel of the post it replaces, #224)."""
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown ledger fields: {sorted(unknown)}")
        found = c.execute("SELECT * FROM ledger WHERE user_id = ? AND source = ? AND path = ? AND platform = ?",
                          (user_id, source, path, platform)).fetchone()
        current = dict(found) if found else {}
        row = {"user_id": user_id, "source": source, "path": path, "platform": platform,
               **{k: current.get(k) for k in FIELDS}, **fields, "synced_at": time.time()}
        c.execute("INSERT OR REPLACE INTO ledger (user_id, source, path, platform, content_hash, post_id, remote_id,"
                  " url, status, message, replaces, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  tuple(row[k] for k in ("user_id", "source", "path", "platform", *FIELDS, "synced_at")))
        return row

    def repoint_entries(self, from_post: str, user_id: str, /, **fields: Any) -> int:
        """Every entry of ``user_id``'s that points at ``from_post`` takes ``fields`` (``post_id`` among them): a person's
        resend of an unconfirmed post makes a new post, and the file that became the old one now has the new one (#226),
        so the next sync follows it instead of making a third. Only the person's own entries: the post is theirs
        (``resend_post`` checked), so no other person's entry names it, and none is ever moved (#234). The number of
        entries changed."""
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown ledger fields: {sorted(unknown)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        return self._exec(f"UPDATE ledger SET {cols}, synced_at = ? WHERE post_id = ? AND user_id = ?", *fields.values(),
                          time.time(), from_post, user_id)

    def follow_post(self, user_id: str, source: str, path: str, platform: str, post_id: str, **fields: Any) -> bool:
        """The entry takes ``fields`` only while it still names ``post_id``, in one statement: another process may have
        re-pointed it since the caller read it (a person's resend, #226), and the new post's entry must not take the
        old one's status. True when it was written."""
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown ledger fields: {sorted(unknown)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        return self._exec(f"UPDATE ledger SET {cols}, synced_at = ? WHERE user_id = ? AND source = ? AND path = ? AND"
                          " platform = ? AND post_id = ?", *fields.values(), time.time(), user_id, source, path, platform,
                          post_id) == 1

    def claim_new(self, user_id: str, source: str, path: str, platform: str, content_hash: str) -> bool:
        """Reserve a brand-new entry before creating a remote post, so two concurrent syncs of the same
        file can't both create one. True if this caller now owns it."""
        now = time.time()
        with self._tx() as c:
            row = c.execute("SELECT status, synced_at, post_id FROM ledger WHERE user_id = ? AND source = ? AND path = ? AND"
                            " platform = ?", (user_id, source, path, platform)).fetchone()
            if row is None:
                c.execute("INSERT INTO ledger (user_id, source, path, platform, content_hash, status, synced_at)"
                          " VALUES (?, ?, ?, ?, ?, 'syncing', ?)", (user_id, source, path, platform, content_hash, now))
                return True
            if row["status"] in ("syncing",) and now - row["synced_at"] < CLAIM_TTL:
                return False
            # An 'unconfirmed' entry that names a post is the person's to decide on (#226); one that names none (from
            # before posts were recorded) has nothing to decide, and an edited file takes it over, as after a cancel.
            if row["status"] in ("syncing", "failed", "missing", "cancelled") or \
                    (row["status"] == "unconfirmed" and row["post_id"] is None):
                # No post until the claim makes its own: a claim cut off before that is taken over (sync._one). An
                # entry that had a post of its own replaced nothing any more; one that never got one (a replace that
                # failed before its row) keeps the post it replaces for the sync taking it over (#280).
                c.execute("UPDATE ledger SET status = 'syncing', content_hash = ?, post_id = NULL, synced_at = ?,"
                          " replaces = CASE WHEN post_id IS NULL THEN replaces ELSE NULL END WHERE"
                          " user_id = ? AND source = ? AND path = ? AND platform = ?",
                          (content_hash, now, user_id, source, path, platform))
                return True
            return False

    def drop_entry(self, user_id: str, source: str, path: str, platform: str) -> None:
        self._exec("DELETE FROM ledger WHERE user_id = ? AND source = ? AND path = ? AND platform = ?",
                   user_id, source, path, platform)

    # ---- Idempotency-Key replay (24 h) ---------------------------------------------

    # A key is reserved (response '') before the work runs, then completed with the response. Reserving is one
    # INSERT OR IGNORE, so of two identical requests exactly one runs; the other is told to retry, then replays the
    # first one's response. A reservation left by a crash is given up after IDEMPOTENCY_PENDING_SECONDS: the next
    # repeat takes the key over. The reservation's ``created_at`` is its owner's token, and only the owner completes or
    # releases it: a request that outlived its reservation leaves the repeat's alone (#416). A repeat takes a key over
    # IDEMPOTENCY_PENDING_SECONDS after it was reserved at the earliest, so its ``created_at`` is never the one it
    # replaced. No column is added: the previous release reserves with a positional INSERT of these five columns, and a
    # rollback (docs/cicd.md) runs it against this database.
    IDEMPOTENCY_SECONDS = 86400
    IDEMPOTENCY_PENDING_SECONDS = 900

    def reserve_idempotency_key(self, user_id: str, key: str, request_hash: str) -> float | tuple[str, str]:
        """The reservation's owner (its ``created_at``: run the request, then `complete_` or `release_` the key with it)
        when this request now holds the key. Otherwise the (request hash, response JSON) of the request holding it; the
        response is '' while that one is running. A person who has no users row any more (erased since the request
        authenticated; issue #196) gets no row and holds nothing, and runs as if they held the key: nothing the request
        writes is kept either."""
        now = time.time()
        with self._tx() as c:
            c.execute("DELETE FROM idempotency WHERE user_id = ? AND key = ? AND (created_at <= ? OR"
                      " (response = '' AND created_at <= ?))",
                      (user_id, key, now - self.IDEMPOTENCY_SECONDS, now - self.IDEMPOTENCY_PENDING_SECONDS))
            if c.execute("INSERT OR IGNORE INTO idempotency (user_id, key, request_hash, response, created_at)"
                         " VALUES (?, ?, ?, '', ?)", (user_id, key, request_hash, now)).rowcount == 1:
                return now
            row = c.execute("SELECT request_hash, response FROM idempotency WHERE user_id = ? AND key = ?",
                            (user_id, key)).fetchone()
        return (row["request_hash"], row["response"]) if row is not None else now

    def complete_idempotency_key(self, user_id: str, key: str, owner: float, response: str) -> bool:
        """Keep ``response`` for replay under the key. False when the reservation is not ``owner``'s any more (a repeat
        took it over after IDEMPOTENCY_PENDING_SECONDS, or the person has no row, #196): it is left as it is."""
        kept = self._exec("UPDATE idempotency SET response = ? WHERE user_id = ? AND key = ? AND created_at = ?",
                          response, user_id, key, owner) == 1
        self._exec("DELETE FROM idempotency WHERE created_at < ?", time.time() - self.IDEMPOTENCY_SECONDS)
        return kept

    def release_idempotency_key(self, user_id: str, key: str, owner: float) -> bool:
        """Free the key so the client can retry with it: the request failed before it had a response, or its response
        asks for a re-run (a ``skipped`` or ``failed`` result, #290). False when the reservation is not ``owner``'s any
        more (taken over, completed, or never written, #196): it is left as it is."""
        return self._exec("DELETE FROM idempotency WHERE user_id = ? AND key = ? AND response = '' AND created_at = ?",
                          user_id, key, owner) == 1

    def ledger_status_counts(self, user_id: str) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM ledger WHERE user_id = ? GROUP BY status ORDER BY status", user_id)
        return {r["status"]: int(r["n"]) for r in rows}

    def list_entries(self, user_id: str, source: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        """The user's entries: of one repository with ``source``, of one of its files (one per platform) with ``path``
        too. Never another user's, and never another repository's: a removal cancels only what its own sync made."""
        if source and path:
            rows = self._all("SELECT * FROM ledger WHERE user_id = ? AND source = ? AND path = ? ORDER BY platform",
                             user_id, source, path)
        elif source:
            rows = self._all("SELECT * FROM ledger WHERE user_id = ? AND source = ? ORDER BY path, platform", user_id, source)
        else:
            rows = self._all("SELECT * FROM ledger WHERE user_id = ? ORDER BY source, path, platform", user_id)
        return [dict(r) for r in rows]
