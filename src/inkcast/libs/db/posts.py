# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Posts (owned by PublishingApi; drained by SchedulerWorker): one row per story per platform.

The table doubles as the durable work queue: ``claim_due_posts`` moves due rows to
``publishing`` atomically, so several scheduler replicas never send the same post twice.
``heartbeats`` lets any process see that the scheduler loop is alive.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Iterable

from ..events import DomainEvent
from .database import Database
from .outbox import insert_events

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL, platform TEXT NOT NULL,
  title TEXT NOT NULL, status TEXT NOT NULL, run_at REAL NOT NULL, payload TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, result_url TEXT, remote_id TEXT, error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_due ON posts(status, run_at);
CREATE INDEX IF NOT EXISTS posts_user ON posts(user_id, run_at);
CREATE TABLE IF NOT EXISTS heartbeats (name TEXT PRIMARY KEY, at REAL NOT NULL);
-- v0.2: "unknown" was renamed "unconfirmed" (the platform may or may not have published).
UPDATE posts SET status = 'unconfirmed' WHERE status = 'unknown';
"""


class PostsRepo(Database):
    def create_post(self, *, group_id: str, user_id: str, platform: str, title: str, status: str,
                    run_at: float, payload: dict[str, Any], created_at: float | None = None,
                    on_created: Callable[[str], None] | None = None) -> str:
        """``created_at`` is now unless the post replaces one whose schedule it keeps (Git sync): then that one's, so
        how late it is counts from when its time was first asked for (``overdue_count``, the lag; ADR-0015).
        ``on_created(post_id)`` runs in the transaction that makes the row (Git sync's ledger write naming it): if it
        raises, there is no row, so none is ever left that nothing names (#213)."""
        post_id = "pst_" + uuid.uuid4().hex[:12]
        now = time.time()
        # A scheduled post is a fact worth an event the moment it exists; a "publishing" one gets its event
        # from the send that follows.
        events = [DomainEvent("post.scheduled", user_id=user_id, subject=post_id,
                              attrs={"platform": platform, "run_at": run_at})] if status == "scheduled" else []
        with self._tx() as c:
            c.execute(
                "INSERT INTO posts (id, group_id, user_id, platform, title, status, run_at, payload, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, group_id, user_id, platform, title, status, run_at, json.dumps(payload),
                 now if created_at is None else created_at, now),
            )
            insert_events(c, events)
            if on_created:
                on_created(post_id)
        return post_id

    def update_post(self, post_id: str, events: Iterable[DomainEvent] | None = None, only_if: str | None = None,
                    **fields: Any) -> None:
        """Change fields; ``events`` are written in the same transaction (the outbox). A post that no longer exists
        (its owner was erased while the scheduler held it) is left alone, and its events are not written either.
        ``only_if``: change it only while it still has this status (another worker may have moved it on)."""
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        status = " AND status = ?" if only_if else ""
        with self._tx() as c:
            if c.execute(f"UPDATE posts SET {cols} WHERE id = ?{status}",
                         (*fields.values(), post_id, *([only_if] if only_if else []))).rowcount == 1:
                insert_events(c, events)

    def cancel_if_scheduled(self, post_id: str, user_id: str, events: Iterable[DomainEvent] | None = None) -> bool:
        """Move one of the user's posts from 'scheduled' to 'cancelled' in a single conditional statement.

        False when the post isn't 'scheduled' any more: the scheduler claimed it a moment ago, or it was already
        cancelled. Checking the status and then updating it separately would let a claim slip in between, and the
        post would go out after the cancel reported success. ``events`` are written only when the cancel happened,
        in the same transaction (the outbox)."""
        with self._tx() as c:
            changed = c.execute(
                "UPDATE posts SET status = 'cancelled', updated_at = ? WHERE id = ? AND user_id = ? AND status = 'scheduled'",
                (time.time(), post_id, user_id),
            ).rowcount == 1
            if changed:
                insert_events(c, events)
        return changed

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM posts WHERE id = ?", post_id)
        return dict(row) if row else None

    # A post whose person has no users row is never the scheduler's: left by a write in the instant of their erasure
    # (issue #196; refused at the write now, and swept from a database from before that), it is neither sent nor
    # recovered, whatever its status.
    _OWNED = " AND EXISTS (SELECT 1 FROM users WHERE users.id = posts.user_id)"

    def claim_due_posts(self, now: float, limit: int = 20) -> list[dict[str, Any]]:
        """Atomically move due scheduled posts to 'publishing' and return them, the one that has waited longest first:
        from its time, or its creation when that came later (``oldest_due_age``), so a Git file dated last year
        doesn't jump ahead of a post that was due before it was synced (ADR-0015); never one whose person has no row."""
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM posts WHERE status = 'scheduled' AND run_at <= ?" + self._OWNED
                + " ORDER BY MAX(run_at, created_at), run_at LIMIT ?", (now, limit)
            ).fetchall()
            for r in rows:
                c.execute(
                    "UPDATE posts SET status = 'publishing', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                    (now, r["id"]),
                )
        return [{**dict(r), "attempts": r["attempts"] + 1} for r in rows]

    def renew_claim(self, post_id: str, attempts: int) -> bool:
        """Just before a claimed post is sent: restart its 'publishing' clock. A batch is sent one post at a time,
        so its tail can wait long enough to look stuck to another worker. False when that worker took the post
        over (``claim_stuck``, or a later claim: ``attempts`` changed), so it must not be sent from here."""
        return self._exec("UPDATE posts SET updated_at = ? WHERE id = ? AND status = 'publishing' AND attempts = ?",
                          time.time(), post_id, attempts) == 1

    def claim_stuck(self, older_than: float) -> list[dict[str, Any]]:
        """Posts left 'publishing' by a crash or restart, moved atomically to 'recovering' so one worker resolves
        each. The caller decides what they become: a send may or may not have reached the platform, so they
        are never simply put back in the queue (that is how schedulers post twice). A post left 'recovering' (that
        worker stopped too) is taken again: recovering only asks the platform, it never sends."""
        with self._tx() as c:
            rows = c.execute("SELECT * FROM posts WHERE status IN ('publishing', 'recovering') AND updated_at < ?"
                             + self._OWNED, (older_than,)).fetchall()
            for r in rows:
                c.execute("UPDATE posts SET status = 'recovering', updated_at = ? WHERE id = ?", (time.time(), r["id"]))
        return [dict(r) for r in rows]

    def post_counts(self) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM posts GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # The queue gauges count what ``claim_due_posts`` would take (``_OWNED``): a post no claim takes is not behind.
    def due_count(self, now: float) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM posts WHERE status = 'scheduled' AND run_at <= ?" + self._OWNED, now)
        return int(row["n"]) if row else 0

    # A post has waited since it could first be sent: its time, or its creation when that came later (a post made
    # after its time, a Git file dated in the past, is due at once, ADR-0015). It is due by its time alone. SQLite's
    # scalar MAX(a, b) is GREATEST(a, b) on PostgreSQL (ADR-0014).

    def oldest_due_age(self, now: float) -> float:
        """Seconds the oldest due, still-scheduled post has waited; 0.0 when nothing is due."""
        row = self._one("SELECT MIN(MAX(run_at, created_at)) AS oldest FROM posts WHERE status = 'scheduled' AND run_at <= ?"
                        + self._OWNED,
                        now)
        return max(0.0, now - float(row["oldest"])) if row and row["oldest"] is not None else 0.0

    def overdue_count(self, now: float, grace: float) -> int:
        """Scheduled posts that have waited more than ``grace`` seconds: the scheduler is behind or stopped."""
        row = self._one("SELECT COUNT(*) AS n FROM posts WHERE status = 'scheduled' AND run_at <= ? AND created_at <= ?"
                        + self._OWNED, now - grace, now - grace)
        return int(row["n"]) if row else 0

    def post_status_counts(self, user_id: str, since: float) -> dict[str, dict[str, int]]:
        """status -> platform -> count, for one user's posts that changed since ``since`` (so a post scheduled
        far ahead counts as scheduled now, and last month's published posts stay out)."""
        rows = self._all("SELECT status, platform, COUNT(*) AS n FROM posts WHERE user_id = ? AND updated_at >= ?"
                         " GROUP BY status, platform ORDER BY status, platform", user_id, since)
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["status"], {})[r["platform"]] = int(r["n"])
        return out

    def list_posts(self, user_id: str, start: float, end: float) -> list[dict[str, Any]]:
        """Posts whose time is up to ``end`` and that could be sent from ``start`` on: a post made after its time (a Git
        file dated last year, ADR-0015) is listed from its creation, so it can be found while it waits and once sent."""
        rows = self._all(
            "SELECT * FROM posts WHERE user_id = ? AND run_at <= ? AND MAX(run_at, created_at) >= ? ORDER BY run_at",
            user_id, end, start,
        )
        return [dict(r) for r in rows]

    # ---- liveness of background loops, visible to every process ----------------

    def beat(self, name: str, at: float | None = None) -> None:
        self._exec("INSERT OR REPLACE INTO heartbeats VALUES (?, ?)", name, at or time.time())

    def last_beat(self, name: str) -> float | None:
        row = self._one("SELECT at FROM heartbeats WHERE name = ?", name)
        return float(row["at"]) if row else None
