# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Posts (owned by PublishingApi; drained by SchedulerWorker): one row per story per platform.

The table doubles as the durable work queue: ``claim_due_posts`` moves due rows to
``publishing`` atomically, so several scheduler replicas never send the same post twice.
``heartbeats`` lets any process see that the scheduler loop is alive.
"""

from __future__ import annotations

import json
import sqlite3
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
# recovering_since: when the post became stuck in 'publishing' (STUCK_AFTER after its send began; ``claim_stuck``
# records it, however late its pass comes), kept while it stays 'recovering' and set afresh by a later interruption;
# meaningful only while the status is 'recovering'. The age of the oldest such post is the
# inkcast_posts_recovering_oldest_age_seconds gauge (the InkcastPostRecoveringTooLong alert), and
# INKCAST_PUBLISH_RECOVERY_MAX_HOURS is measured from it (#216). NULL on a row from before the column: counted from its
# last change until the next stuck pass sets it, from the row's run_at (the earliest its send can have begun).
# requested_at: the time the post was asked for (a Git file's publish_at, a person's scheduled time); NULL for a post
# asked for "now". not_before: the earliest the platform allows it again, written whenever the scheduler re-queues a
# post it has tried (a retry after a 429's Retry-After, or recovery's re-send). A post waiting for its retry has
# run_at = max(requested_at, not_before), so an author's change keeps both (``edit_waiting``, #204, ADR-0017).
# A tried row from before the columns has neither: its run_at is the retry's, and is read as its not_before.
COLUMNS = (("posts", "recovering_since", "REAL"), ("posts", "requested_at", "REAL"), ("posts", "not_before", "REAL"))


class PostsRepo(Database):
    def create_post(self, *, group_id: str, user_id: str, platform: str, title: str, status: str,
                    run_at: float, payload: dict[str, Any], created_at: float | None = None,
                    on_created: Callable[[str], None] | None = None) -> str:
        """``created_at`` is now unless the post replaces one whose schedule it keeps (Git sync): then that one's, so
        how late it is counts from when its time was first asked for (``overdue_count``, the lag; ADR-0015).
        ``on_created(post_id)`` runs in the transaction that makes the row (Git sync's ledger write naming it): if it
        raises, there is no row, so none is ever left that nothing names (#213)."""
        post_id = self.new_post_id()
        now = time.time()
        # A scheduled post is a fact worth an event the moment it exists; a "publishing" one gets its event
        # from the send that follows.
        events = [DomainEvent("post.scheduled", user_id=user_id, subject=post_id,
                              attrs={"platform": platform, "run_at": run_at})] if status == "scheduled" else []
        with self._tx() as c:
            c.execute(
                "INSERT INTO posts (id, group_id, user_id, platform, title, status, run_at, requested_at, payload,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, group_id, user_id, platform, title, status, run_at, run_at if status == "scheduled" else None,
                 json.dumps(payload), now if created_at is None else created_at, now),
            )
            insert_events(c, events)
            if on_created:
                on_created(post_id)
        return post_id

    def edit_waiting(self, post_id: str, user_id: str, *, attempts: int, title: str, payload: dict[str, Any],
                     requested_at: float | None, also: Callable[[sqlite3.Connection], None] | None = None) -> float | None:
        """An author's change to a post the scheduler has tried and that waits for its retry (#204, ADR-0017): the
        same row takes the new content and the new requested time, and keeps its attempt count and the platform's
        wait. Its run_at becomes the later of the time asked for (now, for None) and ``not_before``, so the change
        never sends it before the time written nor before the platform allows.

        Only while it is still 'scheduled' with ``attempts`` sends: the scheduler may have claimed it since the caller
        read it, or tried it again and set a later wait. A post recovery queued again reads the same (no send is
        counted), and its send may still be out: that send's success records what the platform received, and notes
        the change (``record_success``, #392). ``also`` runs on the transaction's connection when the post changed
        (Git sync's ledger entry): if it raises, the change rolls back with it. The new run_at, or None when nothing
        changed."""
        now = time.time()
        with self._tx() as c:
            row = c.execute("SELECT run_at, requested_at, not_before FROM posts WHERE id = ? AND user_id = ?"
                            " AND status = 'scheduled' AND attempts = ?", (post_id, user_id, attempts)).fetchone()
            if row is None:
                return None
            # A run_at later than the time asked for is the wait itself. not_before is read only when the post waits
            # for the time asked for (an earlier edit moved it past the wait), and is otherwise not trusted: a
            # scheduler from before the column (a rolling deploy, a rollback) re-queues a post without writing it.
            asked = row["requested_at"]
            waits_for_asked = asked is not None and abs(row["run_at"] - asked) < 1 and row["not_before"] is not None
            floor = row["not_before"] if waits_for_asked else row["run_at"]
            run_at = max(now if requested_at is None else requested_at, floor)
            c.execute("UPDATE posts SET title = ?, payload = ?, requested_at = ?, not_before = ?, run_at = ?,"
                      " updated_at = ? WHERE id = ?",
                      (title, json.dumps(payload), requested_at, floor, run_at, now, post_id))
            if also is not None:
                also(c)
        return run_at

    def update_post(self, post_id: str, events: Iterable[DomainEvent] | None = None, only_if: str | None = None,
                    unless: str | None = None, if_attempts: int | None = None, unless_found_at: int | None = None,
                    **fields: Any) -> bool:
        """Change fields; ``events`` are written in the same transaction (the outbox). A post that no longer exists
        (its owner was erased while the scheduler held it) is left alone, and its events are not written either.
        ``only_if``: change it only while it still has this status (another worker may have moved it on).
        ``unless``: change it only while it does not have this status (a person cancelled it meanwhile).
        ``if_attempts``: change it only while its ``attempts`` is still this (the fence of a send or a recovery against
        its own late answer: a re-send by another worker counts an attempt, #226).
        ``unless_found_at``: change it unless the platform has confirmed the post at this attempt count ('published',
        'draft' or 'unlisted' with no send since: another worker's lookup found the very send a late answer is about).
        True when the post was changed."""
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        status = ((" AND status = ?" if only_if else "") + (" AND status != ?" if unless else "")
                  + (" AND attempts = ?" if if_attempts is not None else "")
                  + (" AND NOT (status IN ('published', 'draft', 'unlisted') AND attempts = ?)"
                     if unless_found_at is not None else ""))
        with self._tx() as c:
            changed = c.execute(f"UPDATE posts SET {cols} WHERE id = ?{status}",
                                (*fields.values(), post_id, *([only_if] if only_if else []),
                                 *([unless] if unless else []),
                                 *([if_attempts] if if_attempts is not None else []),
                                 *([unless_found_at] if unless_found_at is not None else []))).rowcount == 1
            if changed:
                insert_events(c, events)
        return changed

    CONFIRMED = ("published", "draft", "unlisted")  # the statuses that record a copy on the platform

    @staticmethod
    def same_copy(row: dict[str, Any], remote_id: str | None, url: str | None) -> bool:
        """Whether the copy ``row`` records is the one an answer names. Ids tell two posts apart, so they are compared
        when both have one; the URL only when either has none: a platform may name every post by the account's
        profile URL (the review of #390). The column is text, so an id a plug-in hands on as a number is compared as
        the text it is stored as, as SQL's affinity compared it before."""
        if row["remote_id"] is not None and remote_id is not None:
            return row["remote_id"] == str(remote_id)
        return row["result_url"] is not None and row["result_url"] == url

    @staticmethod
    def may_have_arrived_since(row: dict[str, Any], held: int) -> bool:
        """Whether ``row`` counts a send since ``held`` (another worker's re-send) that may have reached the platform:
        still out, or answered without saying. Queued again ('scheduled') or 'failed' say it certainly did not."""
        return row["attempts"] != held and row["status"] not in ("scheduled", "failed")

    @staticmethod
    def holds(row: dict[str, Any], sent: dict[str, Any]) -> bool:
        """Whether ``row`` still holds the content a send carried (``sent``, its ``Prepared.to_dict()``), compared as the
        row stores it (through JSON, as ``create_post`` writes it). An author's change while the send was out gave the
        row other content: Git sync's edit in place of a post recovery had queued again (``edit_waiting``, #392)."""
        return json.loads(row["payload"]) == json.loads(json.dumps(sent))

    def record_success(self, post_id: str, *, held: int, status: str, result_url: str | None, remote_id: str | None,
                       sent: dict[str, Any], notes: list[str], second_copy: Callable[[int], str],
                       both_copies: Callable[[dict[str, Any]], str], edited: str,
                       events: Iterable[DomainEvent] | None = None) -> tuple[bool, dict[str, Any] | None]:
        """A send's success, the platform's truth: written over whatever the post became while the send was out
        (#214), unless the row already records a confirmed copy (``CONFIRMED``, with a remote id or a URL) that is not
        this one (``same_copy``). That copy stays, another send's or a lookup's, and the row's error gains
        ``both_copies(row)``: the platform may have both, and a person keeps one. When the row counts a later send that
        may have arrived (``may_have_arrived_since``), ``second_copy(attempts)`` joins ``notes`` as the error of the
        written post, so a person checks for two. The content the platform received, ``sent``, becomes the row's title
        and payload: a row that held other content (``holds``; an edit while the send was out, #392) gains ``edited``
        as a note, and the file's entry takes the hash of what the row holds when it follows the post (``GitSync._one``),
        so the next sync sends the edit or reports it. The read that words any note and the write share the
        transaction's write lock (``_tx``), so a claim or an edit can't land between them and leave a record without its
        note. ``events`` are written with a success. ``(written, the row as it was)``; ``(False, None)`` for a post that
        no longer exists (its owner erased), which is left alone."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
            if row is None:
                return False, None
            before = dict(row)
            now = time.time()
            recorded = before["status"] in self.CONFIRMED and (before["remote_id"] is not None
                                                               or before["result_url"] is not None)
            if recorded and not self.same_copy(before, remote_id, result_url):
                note = both_copies(before)
                c.execute("UPDATE posts SET error = ?, updated_at = ? WHERE id = ?",
                          (f"{before['error']}; {note}" if before["error"] else note, now, post_id))
                return False, before
            if self.may_have_arrived_since(before, held):
                notes = [*notes, second_copy(before["attempts"])]
            if not self.holds(before, sent):
                notes = [*notes, edited]
            c.execute("UPDATE posts SET status = ?, result_url = ?, remote_id = ?, error = ?, title = ?, payload = ?,"
                      " updated_at = ? WHERE id = ?",
                      (status, result_url, remote_id, "; ".join(notes) or None, sent["title"], json.dumps(sent), now,
                       post_id))
            insert_events(c, events)
        return True, before

    PENDING = ("scheduled", "recovering", "unconfirmed")  # the statuses a person can cancel from (#216, #226)

    def cancel_if_pending(self, post_id: str, user_id: str, events: Iterable[DomainEvent] | None = None,
                          notes: dict[str, str] | None = None,
                          also: Callable[[sqlite3.Connection], None] | None = None) -> str | None:
        """Move one of the user's posts from 'scheduled', 'recovering' or 'unconfirmed' to 'cancelled', in one
        transaction. A 'recovering' post (its send was interrupted, and the platform has not said whether it arrived)
        is the person's to cancel too (#216), and so is an 'unconfirmed' one (#226): recovery writes its answer only
        while the post is still 'recovering' (``update_post(only_if=)``), so a cancel wins over an answer that lands
        after it, and the sender's late failure yields to a cancel (``unless=``).

        The status the post was cancelled from, or None when it was none of those any more: the scheduler claimed it
        a moment ago, it was already cancelled, or recovery resolved it. The read and the write share the transaction's
        write lock, so a claim can't slip in between them and send the post after the cancel reported success.
        ``events`` are written only when the cancel happened, in the same transaction (the outbox). ``notes`` maps a
        status to the error that replaces the post's (what recovery said it would do next, why it was unconfirmed)
        when cancelled from it: a post that may already be on the platform says so. ``also`` is a write that belongs
        with the cancel, run on the transaction's connection only when the cancel happened: if it raises, the cancel and
        its events roll back with it (Git sync's claim on the file whose post it replaces, #224)."""
        with self._tx() as c:
            row = c.execute("SELECT status FROM posts WHERE id = ? AND user_id = ?", (post_id, user_id)).fetchone()
            was = row["status"] if row is not None else None
            if was not in self.PENDING:
                return None
            c.execute("UPDATE posts SET status = 'cancelled', updated_at = ?, error = COALESCE(?, error) WHERE id = ?",
                      (time.time(), (notes or {}).get(was), post_id))
            insert_events(c, events)
            if also is not None:
                also(c)
        return was

    @staticmethod
    def new_post_id() -> str:
        return "pst_" + uuid.uuid4().hex[:12]

    def resend_post(self, post_id: str, user_id: str, *, new_id: str, note: str,
                    events: Iterable[DomainEvent] | None = None, on_created: Callable[[str], None] | None = None) -> bool:
        """A person's decision on one of their 'unconfirmed' posts (#226): a new post row ``new_id``, 'publishing' and
        due now, with the old post's group, platform, title and payload; and the old post 'cancelled', with ``note``
        as its error (it may be on the platform already). Both in one transaction, conditional on the old post still
        being 'unconfirmed', so two resends at once make exactly one post and a cancel that came first holds.
        ``events`` (the decision, with who made it) are written with them, and ``on_created(new_id)`` runs in the same
        transaction (the ledger entries that named the old post now name the new one): if it raises, nothing is made.
        False when nothing was made: the post is not the person's, or not 'unconfirmed' any more (cancelled, or
        resent already)."""
        now = time.time()
        with self._tx() as c:
            row = c.execute("SELECT * FROM posts WHERE id = ? AND user_id = ? AND status = 'unconfirmed'",
                            (post_id, user_id)).fetchone()
            if row is None:
                return False
            c.execute(
                "INSERT INTO posts (id, group_id, user_id, platform, title, status, run_at, payload, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'publishing', ?, ?, ?, ?)",
                (new_id, row["group_id"], user_id, row["platform"], row["title"], now, row["payload"], now, now),
            )
            c.execute("UPDATE posts SET status = 'cancelled', error = ?, updated_at = ? WHERE id = ?", (note, now, post_id))
            insert_events(c, events)
            if on_created:
                on_created(new_id)
        return True

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

    def claim_stuck(self, older_than: float, now: float | None = None) -> list[dict[str, Any]]:
        """Posts left 'publishing' by a crash or restart, moved atomically to 'recovering' so one worker resolves
        each. The caller decides what they become: a send may or may not have reached the platform, so they
        are never simply put back in the queue (that is how schedulers post twice). A post left 'recovering' (that
        worker stopped too, or the platform refused its lookup) is taken again: recovering only asks the platform,
        it never sends. Each row comes back with its ``recovering_since``: when a post taken from 'publishing'
        became stuck (at most ``now``), kept for one taken again (#216) and for one re-sent by recovery and
        interrupted again (#226), from its run_at for one from before the column."""
        now = now or time.time()
        out = []
        with self._tx() as c:
            rows = c.execute("SELECT * FROM posts WHERE status IN ('publishing', 'recovering') AND updated_at < ?"
                             + self._OWNED, (older_than,)).fetchall()
            for r in rows:
                # Since it became stuck (its send began ``now - older_than`` before that), not since this pass. A pass
                # that comes late (no scheduler ran: an outage, a crash loop) must not restart the cap's clock, or the
                # post goes out that much later on its own (#216). Nor does a re-entry: a post recovery re-queued (the
                # platform said it was missing) whose new send was interrupted too keeps the clock of its first
                # interruption, so its lateness as a whole is what the cap bounds (#226); only a person's decision (a
                # resend is a new post) starts afresh. A post 'recovering' from before the column has no clock: its
                # send began no earlier than its run_at.
                if r["recovering_since"] is not None:
                    since = r["recovering_since"]
                else:
                    began = r["run_at"] if r["status"] == "recovering" else r["updated_at"]
                    since = min(now, began + (now - older_than))
                c.execute("UPDATE posts SET status = 'recovering', updated_at = ?, recovering_since = ? WHERE id = ?",
                          (now, since, r["id"]))
                out.append({**dict(r), "recovering_since": since})
        return out

    def oldest_recovering_age(self, now: float) -> float:
        """Seconds the oldest 'recovering' post has been so (a row from before ``recovering_since``: since its last
        change); 0.0 when none is. The inkcast_posts_recovering_oldest_age_seconds gauge (#216)."""
        row = self._one("SELECT MIN(COALESCE(recovering_since, updated_at)) AS oldest FROM posts WHERE status = 'recovering'")
        return max(0.0, now - float(row["oldest"])) if row and row["oldest"] is not None else 0.0

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
