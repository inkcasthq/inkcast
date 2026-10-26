# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Git publishing: make each platform match the markdown files in a repository, without duplicates.

For every (file, platform) the publish ledger remembers what was last sent (a hash of the
rendered payload) and what it became (post id, remote id, URL). A sync then decides:

=========  ======================================================================
create     no ledger entry: publish, or schedule at the time as written if the file has ``publish_at`` (ADR-0015)
unchanged  same rendered payload as last time: do nothing
update     payload changed and the platform can edit posts (DEV): edit in place;
           a post still waiting in the schedule is re-scheduled with the new content
stale      payload changed but the platform can't edit (Medium): leave it, say so
skipped    platform not connected, or another sync of this file is in progress
failed     validation or platform error; the next sync tries again
=========  ======================================================================

A brand-new entry is claimed in the ledger *before* anything is sent, so two CI runs racing on
the same file can't both create a post. An ``unconfirmed`` entry (the platform may or may not
have published) is checked with ``Platform.find_existing`` before anything else happens.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

from ...libs.content.story import SCHEDULE_LEAD, StoryError, parse_story
from ...libs.contracts.models import LedgerEntry, SyncRequest, SyncResponse, SyncResult
from ...libs.db import Store
from ...libs.db.ledger import CLAIM_TTL
from ...libs.events import DomainEvent
from ...libs.observability import Metrics
from ...libs.pipeline import Publisher
from ...libs.platforms import Platform, PlatformError, Prepared


def payload_hash(prepared: Prepared) -> str:
    """What the platform would receive. The schedule time is deliberately not part of it: once a post is
    out, a date in its front matter no longer matters (see ``_one`` for still-scheduled posts)."""
    data = prepared.to_dict()
    data.pop("warnings", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class GitSync:
    def __init__(self, store: Store, platforms: dict[str, Platform], publisher: Publisher, metrics: Metrics | None = None):
        self.store, self.platforms, self.publisher, self.metrics = store, platforms, publisher, metrics

    def _count(self, platform: str, action: str) -> None:
        if self.metrics:
            self.metrics.inc("inkcast_sync_total", platform=platform, action=action)

    async def sync(self, user_id: str, req: SyncRequest) -> SyncResponse:
        results: list[SyncResult] = []
        for f in req.files:
            results += await self._file(user_id, req, f.path, f.markdown, f.image_base_url or req.image_base_url)
        for r in results:
            self._count(r.platform, r.action)
        if not req.dry_run:
            self.store.enqueue(*(DomainEvent("sync.result", user_id=user_id, subject=f"{req.source}:{r.path}",
                                             attrs={"platform": r.platform, "action": r.action, "source": req.source})
                                 for r in results))
        return SyncResponse(source=req.source, ref=req.ref, dry_run=req.dry_run, results=results)

    async def _file(self, user_id: str, req: SyncRequest, path: str, markdown: str,
                    image_base_url: str | None) -> list[SyncResult]:
        try:
            # A file's publish_at is kept as written, passed or not: the post is scheduled at that time, and the
            # scheduler sends it (ADR-0015). Only a file with no publish_at is a "now" post.
            story = parse_story(markdown, known_platforms=self.platforms.keys(), platforms=req.platforms,
                                image_base_url=image_base_url, schedule_as_written=True)
        except StoryError as exc:
            return [SyncResult(path=path, platform="*", action="failed", message=str(exc))]
        connected = self.store.connected_accounts(user_id)
        targets = story.platforms or [pid for pid in connected if pid in self.platforms]
        if not targets:
            return [SyncResult(path=path, platform="*", action="skipped",
                               message="No platforms: set `platforms:` in the front matter or connect one.")]
        out = []
        for pid in targets:
            platform = self.platforms[pid]
            try:
                prepared = platform.prepare(story.for_platform(pid), story.body)
            except PlatformError as exc:
                out.append(SyncResult(path=path, platform=pid, action="failed", message=str(exc)))
                continue
            if pid not in connected:
                help_text = platform.not_connected_help(story.for_platform(pid))
                out.append(SyncResult(path=path, platform=pid, action="skipped",
                                      message=f"{platform.name} isn't connected." + (f" {help_text}" if help_text else "")))
                continue
            out.append(await self._one(user_id, req, path, story, platform, prepared))
        return out

    async def _one(self, user_id: str, req: SyncRequest, path: str, story, platform: Platform,
                   prepared: Prepared) -> SyncResult:
        src, pid = req.source, platform.id
        digest = payload_hash(prepared)
        entry = self.store.get_entry(user_id, src, path, pid)
        base = {"path": path, "platform": pid}

        # An abandoned claim that already has its post: the sync stopped while sending it. Follow that post (the
        # scheduler recovers it) instead of letting the claim be taken over and the post created a second time.
        if entry and entry["status"] == "syncing" and entry["post_id"] and time.time() - entry["synced_at"] >= CLAIM_TTL:
            entry = self.store.put_entry(user_id, src, path, pid, status="publishing")

        # A scheduled post may have gone out (or failed) since the last sync: take its current state.
        post = self.store.get_post(entry["post_id"]) if entry and entry["post_id"] else None
        if post and entry["status"] in ("scheduled", "publishing", "recovering") and post["status"] != entry["status"]:
            entry = self.store.put_entry(user_id, src, path, pid, status=post["status"], url=post["result_url"],
                                         remote_id=post["remote_id"])

        if entry and entry["status"] == "unconfirmed" and not req.dry_run:
            entry = await self._confirm(user_id, src, path, platform, prepared, entry)
            if entry is None:
                return SyncResult(**base, action="skipped", status="unconfirmed", message=(
                    f"The last publish to {platform.name} may or may not have gone through, and {platform.name} can't "
                    f"be checked automatically. Check {platform.name}; then remove this file's ledger entry or edit "
                    "the file to try again."))

        changed = entry is not None and entry["content_hash"] != digest
        if entry is None or entry["status"] in ("failed", "syncing", "missing") or \
                (entry["status"] == "cancelled" and changed):
            return await self._create(user_id, req, path, story, platform, prepared, digest, entry)

        schedule_changed = False
        if post and post["status"] == "scheduled":  # not sent yet: the schedule time matters too
            wanted = story.publish_at.timestamp() if story.publish_at else None  # the time as written (ADR-0015)
            if post["attempts"]:
                # A post the scheduler has tried waits for its retry (INKCAST_PUBLISH_RETRY_DELAYS): its run_at is the
                # retry's, so only a new time still ahead in the file is a change. A time that has passed, or is under
                # SCHEDULE_LEAD away, has not changed: it is the file's own, which the scheduler passed when it tried
                # the post (the lead is the tolerance between its clock and this one). A time later than the retry is
                # a change however near: the retry would send the post before it (ADR-0015).
                ahead = wanted is not None and (wanted >= time.time() + SCHEDULE_LEAD.total_seconds() or
                                                wanted > post["run_at"])
                schedule_changed = ahead and abs(post["run_at"] - wanted) >= 1
            else:
                # Not tried yet: its run_at is the time the file asked for. Compare with that, whatever the clock: in
                # the minutes before it, or once it has passed (the scheduler behind), an unchanged file has not
                # changed, and the post is the scheduler's to send at its time (#187).
                schedule_changed = wanted is None or abs(post["run_at"] - wanted) >= 1
            changed = changed or schedule_changed

        if not changed:
            if entry["message"] and not req.dry_run:  # e.g. a "stale" note, now resolved by reverting the file
                self.store.put_entry(user_id, src, path, pid, message=None)
            return SyncResult(**base, action="unchanged", status=entry["status"], post_id=entry["post_id"],
                              url=entry["url"])

        if post and post["status"] == "scheduled":  # not sent yet: replace what will be sent
            if req.dry_run:
                return SyncResult(**base, action="update", status="scheduled", post_id=post["id"], preview=prepared.preview,
                                  message="Would re-schedule with the new content.")
            try:
                self.publisher.cancel(user_id, post["id"])
            except PlatformError:  # it went out, or another sync replaced it, a moment ago
                return SyncResult(**base, action="skipped", status=entry["status"], post_id=post["id"],
                                  message="The scheduled post changed state during this sync; run the sync again.")
            # Ours to replace now: hold it as a claim, or a concurrent sync sees the cancelled post and creates one too.
            # The replacement is scheduled at the time the file asks for, as written: passed or under SCHEDULE_LEAD
            # away, it is due at once, as a scheduled post the scheduler retries under its policy, never a "now" post
            # sent early and never retried (#187, ADR-0015). Only a file whose publish_at was removed sends now.
            # The claim names no post until its own exists (on_record): stopped before that, it is taken over, never
            # followed to the cancelled one.
            self.store.put_entry(user_id, src, path, pid, status="syncing", post_id=None)
            return await self._create(user_id, req, path, story, platform, prepared, digest, entry, replace=True)

        if "update" not in platform.capabilities or not entry["remote_id"]:
            message = (f"Changed since it was published, but {platform.name} doesn't let Inkcast edit posts. "
                       f"Update it by hand" + (f": {entry['url']}" if entry["url"] else ".") +
                       " Inkcast won't post it again.")
            if not req.dry_run:
                self.store.put_entry(user_id, src, path, pid, message=message)
            return SyncResult(**base, action="stale", status=entry["status"], post_id=entry["post_id"], url=entry["url"],
                              message=message, preview=prepared.preview if req.dry_run else None)

        if req.dry_run:
            return SyncResult(**base, action="update", status=entry["status"], post_id=entry["post_id"], url=entry["url"],
                              preview=prepared.preview)
        o = await self.publisher.update(user_id, entry["post_id"], platform, prepared, entry["remote_id"])
        if o.status == "failed" or o.status == "skipped":
            return SyncResult(**base, action="failed" if o.status == "failed" else "skipped", status=entry["status"],
                              post_id=entry["post_id"], url=entry["url"], message=o.message)
        self.store.put_entry(user_id, src, path, pid, content_hash=digest, status=o.status, url=o.url or entry["url"],
                             remote_id=o.remote_id or entry["remote_id"], message=None)
        return SyncResult(**base, action="update", status=o.status, post_id=entry["post_id"], url=o.url or entry["url"],
                          message="; ".join(o.notes) or None)

    async def _create(self, user_id: str, req: SyncRequest, path: str, story, platform: Platform, prepared: Prepared,
                      digest: str, entry: dict | None, replace: bool = False) -> SyncResult:
        src, pid = req.source, platform.id
        base = {"path": path, "platform": pid}
        action = "update" if replace else "create"
        if req.dry_run:
            return SyncResult(**base, action=action, status="scheduled" if story.publish_at else "preview",
                              preview=prepared.preview)
        if not replace and not self.store.claim_new(user_id, src, path, pid, digest):
            return SyncResult(**base, action="skipped", message="Another sync of this file is in progress.")

        def on_record(_pid: str, post_id: str) -> None:  # before the send, so a crash mid-send can't orphan the post
            self.store.put_entry(user_id, src, path, pid, post_id=post_id)

        try:
            [o] = await self.publisher.publish(user_id, story, [pid], on_created=on_record)
        except Exception:
            if not replace:
                self.store.put_entry(user_id, src, path, pid, status="failed", message="internal error")
            raise
        if o.status in ("failed", "skipped"):
            if replace:
                self.store.put_entry(user_id, src, path, pid, status="failed", message=o.message)
            else:
                self.store.put_entry(user_id, src, path, pid, status="failed", content_hash=digest, message=o.message)
            return SyncResult(**base, action="failed" if o.status == "failed" else "skipped", message=o.message)
        self.store.put_entry(user_id, src, path, pid, content_hash=digest, post_id=o.post_id, status=o.status,
                             url=o.url, remote_id=o.remote_id, message=o.message if o.status == "unconfirmed" else None)
        return SyncResult(**base, action=action, status=o.status, post_id=o.post_id, url=o.url,
                          message=o.message or ("; ".join(o.notes) or None))

    async def _confirm(self, user_id: str, src: str, path: str, platform: Platform, prepared: Prepared,
                       entry: dict) -> dict | None:
        """Resolve an unconfirmed entry: published → record it; missing → allow a fresh create; None → can't tell."""
        o = await self.publisher.confirm(user_id, platform, prepared)
        if o is None:
            return None
        if o.status == "missing":
            if entry["post_id"]:
                self.store.update_post(entry["post_id"], status="failed",
                                       error=f"Not found on {platform.name}; it was never published.")
            return self.store.put_entry(user_id, src, path, platform.id, status="missing", message=None)
        if entry["post_id"]:
            self.store.update_post(entry["post_id"], status=o.status, result_url=o.url, remote_id=o.remote_id,
                                   error=None)
        return self.store.put_entry(user_id, src, path, platform.id, status=o.status, url=o.url, remote_id=o.remote_id,
                                    message=None)

    def ledger(self, user_id: str, source: str | None = None) -> list[LedgerEntry]:
        return [LedgerEntry(source=r["source"], path=r["path"], platform=r["platform"], status=r["status"],
                            content_hash=r["content_hash"], post_id=r["post_id"], url=r["url"], message=r["message"],
                            synced_at=datetime.fromtimestamp(r["synced_at"], timezone.utc).isoformat(timespec="seconds"))
                for r in self.store.list_entries(user_id, source)]
