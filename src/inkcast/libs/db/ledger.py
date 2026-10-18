# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Publish ledger (owned by PublishingApi): which file in which repository became which remote post.

One row per (user, source, path, platform). ``content_hash`` is the hash of what was last sent,
so a re-run with unchanged content is a no-op and a changed file updates the post instead of
creating a duplicate.
"""

from __future__ import annotations

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

CLAIM_TTL = 600  # a 'syncing' claim older than this was left by a crash and may be taken over

FIELDS = ("content_hash", "post_id", "remote_id", "url", "status", "message")


class LedgerRepo(Database):
    def get_entry(self, user_id: str, source: str, path: str, platform: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM ledger WHERE user_id = ? AND source = ? AND path = ? AND platform = ?",
                        user_id, source, path, platform)
        return dict(row) if row else None

    def put_entry(self, user_id: str, source: str, path: str, platform: str, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown ledger fields: {sorted(unknown)}")
        current = self.get_entry(user_id, source, path, platform) or {}
        row = {"user_id": user_id, "source": source, "path": path, "platform": platform,
               **{k: current.get(k) for k in FIELDS}, **fields, "synced_at": time.time()}
        self._exec("INSERT OR REPLACE INTO ledger (user_id, source, path, platform, content_hash, post_id, remote_id,"
                   " url, status, message, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   *(row[k] for k in ("user_id", "source", "path", "platform", *FIELDS, "synced_at")))
        return row

    def claim_new(self, user_id: str, source: str, path: str, platform: str, content_hash: str) -> bool:
        """Reserve a brand-new entry before creating a remote post, so two concurrent syncs of the same
        file can't both create one. True if this caller now owns it."""
        now = time.time()
        with self._tx() as c:
            row = c.execute("SELECT status, synced_at FROM ledger WHERE user_id = ? AND source = ? AND path = ? AND"
                            " platform = ?", (user_id, source, path, platform)).fetchone()
            if row is None:
                c.execute("INSERT INTO ledger (user_id, source, path, platform, content_hash, status, synced_at)"
                          " VALUES (?, ?, ?, ?, ?, 'syncing', ?)", (user_id, source, path, platform, content_hash, now))
                return True
            if row["status"] in ("syncing",) and now - row["synced_at"] < CLAIM_TTL:
                return False
            if row["status"] in ("syncing", "failed", "missing", "cancelled"):
                c.execute("UPDATE ledger SET status = 'syncing', content_hash = ?, synced_at = ? WHERE user_id = ? AND"
                          " source = ? AND path = ? AND platform = ?", (content_hash, now, user_id, source, path, platform))
                return True
            return False

    def drop_entry(self, user_id: str, source: str, path: str, platform: str) -> None:
        self._exec("DELETE FROM ledger WHERE user_id = ? AND source = ? AND path = ? AND platform = ?",
                   user_id, source, path, platform)

    # ---- Idempotency-Key replay (24 h) ---------------------------------------------

    # A key is reserved (response '') before the work runs, then completed with the response. Reserving is one
    # INSERT OR IGNORE, so of two identical requests exactly one runs; the other is told to retry, then replays the
    # first one's response. A reservation left by a crash is given up after IDEMPOTENCY_PENDING_SECONDS.
    IDEMPOTENCY_SECONDS = 86400
    IDEMPOTENCY_PENDING_SECONDS = 900

    def reserve_idempotency_key(self, user_id: str, key: str, request_hash: str) -> tuple[str, str] | None:
        """None when this request now holds the key (run it, then `complete_` or `release_` it). Otherwise the
        (request hash, response JSON) of the request holding it; the response is '' while that one is running."""
        now = time.time()
        with self._tx() as c:
            c.execute("DELETE FROM idempotency WHERE user_id = ? AND key = ? AND (created_at <= ? OR"
                      " (response = '' AND created_at <= ?))",
                      (user_id, key, now - self.IDEMPOTENCY_SECONDS, now - self.IDEMPOTENCY_PENDING_SECONDS))
            if c.execute("INSERT OR IGNORE INTO idempotency VALUES (?, ?, ?, '', ?)",
                         (user_id, key, request_hash, now)).rowcount == 1:
                return None
            row = c.execute("SELECT request_hash, response FROM idempotency WHERE user_id = ? AND key = ?",
                            (user_id, key)).fetchone()
        return row["request_hash"], row["response"]

    def complete_idempotency_key(self, user_id: str, key: str, response: str) -> None:
        self._exec("UPDATE idempotency SET response = ? WHERE user_id = ? AND key = ?", response, user_id, key)
        self._exec("DELETE FROM idempotency WHERE created_at < ?", time.time() - self.IDEMPOTENCY_SECONDS)

    def release_idempotency_key(self, user_id: str, key: str) -> None:
        """The request failed before it had a response: free the key so the client can retry with it."""
        self._exec("DELETE FROM idempotency WHERE user_id = ? AND key = ? AND response = ''", user_id, key)

    def ledger_status_counts(self, user_id: str) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM ledger WHERE user_id = ? GROUP BY status ORDER BY status", user_id)
        return {r["status"]: int(r["n"]) for r in rows}

    def list_entries(self, user_id: str, source: str | None = None) -> list[dict[str, Any]]:
        if source:
            rows = self._all("SELECT * FROM ledger WHERE user_id = ? AND source = ? ORDER BY path, platform", user_id, source)
        else:
            rows = self._all("SELECT * FROM ledger WHERE user_id = ? ORDER BY source, path, platform", user_id)
        return [dict(r) for r in rows]
