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
removed    the file is gone from the repository (``SyncRequest.removed``): its pending post is cancelled;
           a published one stays on the platform (``_removed``, #230)
=========  ======================================================================

A brand-new entry is claimed in the ledger *before* anything is sent, so two CI runs racing on
the same file can't both create a post. An ``unconfirmed`` entry (the platform may or may not
have published) is checked with ``Platform.find_existing`` before anything else happens. Once a
post row exists, an error (the database locked on the write after the send, say) leaves the entry
on that post with the post's own status, never ``failed``, which the next sync would create again:
the post's status decides what happens next (``_after_error``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

from ...libs.content.story import StoryError, parse_story
from ...libs.contracts.models import LedgerEntry, SyncRequest, SyncResponse, SyncResult
from ...libs.db import Store
from ...libs.db.ledger import CLAIM_TTL
from ...libs.events import DomainEvent
from ...libs.observability import Metrics
from ...libs.pipeline import Outcome, Publisher
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
        for path in req.removed:
            results += self._removed(user_id, req, path)
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

        # A scheduled post may have gone out (or failed) since the last sync, and a person may have cancelled or resent
        # an unconfirmed one (#226): take its current state.
        post = self.store.get_post(entry["post_id"]) if entry and entry["post_id"] else None
        if post and entry["status"] in ("scheduled", "publishing", "recovering", "unconfirmed") \
                and post["status"] != entry["status"]:
            if not self.store.follow_post(user_id, src, path, pid, post["id"], status=post["status"],
                                          url=post["result_url"], remote_id=post["remote_id"]):
                # Re-pointed since this sync read it (a person's resend, in another process): never the old post's
                # status on the new post's entry. The next sync follows the post it names now.
                now = self.store.get_entry(user_id, src, path, pid) or entry
                return SyncResult(**base, action="skipped", status=now["status"], post_id=now["post_id"], message=(
                    "This file's post changed during this sync (a person resent it); run the sync again."))
            entry = self.store.get_entry(user_id, src, path, pid) or entry

        if entry and entry["status"] == "unconfirmed" and req.dry_run and post is not None:
            # A dry run asks the platform nothing, but says what the real run does: the person decides (#226).
            return SyncResult(**base, action="skipped", status="unconfirmed", post_id=post["id"],
                              message=self._decision(platform, post["id"], None, dry_run=True))
        if entry and entry["status"] == "unconfirmed" and not req.dry_run:
            checked = await self.publisher.confirm(user_id, platform, prepared)
            if checked is not None and checked.status == "unavailable":  # rate-limited, unreachable: ask again next time
                if post is not None:  # the person's to decide meanwhile, like any unconfirmed post (#226)
                    return SyncResult(**base, action="skipped", status="unconfirmed", post_id=post["id"],
                                      message=self._decision(platform, post["id"], checked))
                return SyncResult(**base, action="skipped", status="unconfirmed", post_id=entry["post_id"], message=(
                    f"The last publish to {platform.name} may or may not have gone through, and {platform.name} couldn't "
                    f"be asked just now ({checked.message}). The next sync will ask again."))
            if post is not None and (checked is None or checked.status == "missing"):
                # The entry points at an unconfirmed post (an uncertain answer to its send, or recovery capped at
                # INKCAST_PUBLISH_RECOVERY_MAX_HOURS): a person decides, by resending or cancelling it. The sync never
                # creates it again, whatever the platform says (#226): "missing" could be a lookup that lags the post,
                # and a post is never sent late without a person deciding. A post the platform does have is recorded.
                return SyncResult(**base, action="skipped", status="unconfirmed", post_id=post["id"],
                                  message=self._decision(platform, post["id"], checked))
            entry, unresolved = self._resolve(user_id, src, path, platform, entry, checked), entry
            if entry is None:
                # No post to decide on (an entry from before posts were recorded): an edited file is a new post, as
                # after a cancel (``claim_new`` takes such an entry over); an unchanged one waits for that edit (#226).
                if unresolved["content_hash"] != digest:
                    return await self._create(user_id, req, path, story, platform, prepared, digest, unresolved)
                return SyncResult(**base, action="skipped", status="unconfirmed", message=(
                    f"The last publish to {platform.name} may or may not have gone through, and {platform.name} can't "
                    f"be checked automatically. Check {platform.name}; an edit of this file is published as a new post, so "
                    f"remove the first copy on {platform.name} if it did arrive."))

        changed = entry is not None and entry["content_hash"] != digest
        if entry is None or entry["status"] in ("failed", "syncing", "missing") or \
                (entry["status"] == "cancelled" and changed):
            return await self._create(user_id, req, path, story, platform, prepared, digest, entry)

        schedule_changed = False
        if post and post["status"] == "scheduled":  # not sent yet: the schedule time matters too
            wanted = story.publish_at.timestamp() if story.publish_at else None  # the time as written (ADR-0015)
            if post["attempts"]:
                # A post the scheduler has tried waits for its retry: its run_at is the retry's, so compare with the
                # time it was asked for (None: now). A row from before requested_at has none, and reads as changed
                # once if the file has a time: the edit in place keeps its retry, so nothing is sent sooner (#204).
                asked = post["requested_at"]
                schedule_changed = (wanted is None) != (asked is None) or \
                    (wanted is not None and asked is not None and abs(asked - wanted) >= 1)
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

        if post and post["status"] == "scheduled" and post["attempts"]:
            return await self._edit_waiting(user_id, req, path, platform, prepared, digest, post, story)

        if post and post["status"] == "scheduled":  # not sent yet: replace what will be sent
            if req.dry_run:
                return SyncResult(**base, action="update", status="scheduled", post_id=post["id"], preview=prepared.preview,
                                  message="Would re-schedule with the new content.")
            # Ours to replace now: hold it as a claim, or a concurrent sync sees the cancelled post and creates one too.
            # The claim is written in the cancel's transaction (#224): a claim that fails rolls the cancel back, so the
            # entry is never left on a post the author didn't cancel, which the next sync would follow and never send.
            # The claim names no post until its own exists (on_record): stopped before that, it is taken over, never
            # followed to the cancelled one. Through the publisher's store, as on_record: ``c`` is its connection.
            # Only while the scheduler still hasn't tried it: a claim since this sync read it (every send counts one)
            # left it waiting for its retry, changed in place and never replaced (ADR-0017), or unconfirmed, the
            # person's to decide (#226). Raising here rolls the cancel back.
            def claim(c: sqlite3.Connection) -> None:
                if c.execute("SELECT attempts FROM posts WHERE id = ?", (post["id"],)).fetchone()["attempts"] != post["attempts"]:
                    raise PlatformError("The scheduler tried the post during this sync.")
                self.publisher.store.put_entry_in(c, user_id, src, path, pid, status="syncing", post_id=None)

            try:
                self.publisher.cancel(user_id, post["id"], also=claim)
            except PlatformError:  # it went out, the scheduler tried it, or another sync replaced it, a moment ago
                return SyncResult(**base, action="skipped", status=entry["status"], post_id=post["id"],
                                  message="The scheduled post changed state during this sync; run the sync again.")
            # The replacement is scheduled at the time the file asks for, as written: passed or under SCHEDULE_LEAD
            # away, it is due at once, as a scheduled post the scheduler retries under its policy, never a "now" post
            # sent early and never retried (#187, ADR-0015). Only a file whose publish_at was removed sends now.
            # Only the content changed, and the scheduler hasn't tried the post: the replacement keeps its schedule, so
            # it keeps its creation too, and a post the scheduler is behind on stays as late as it is (ADR-0015).
            kept = post["created_at"] if not schedule_changed and not post["attempts"] else None
            return await self._create(user_id, req, path, story, platform, prepared, digest, entry, replace=True,
                                      created_at=kept)

        if post and post["status"] == "recovering":
            # Its send was interrupted and the platform has not said whether it arrived, so it is neither replaced
            # (that could post it twice) nor "stale": a person decides, by cancelling it (#216), after which the next
            # sync creates the new version as it does for any cancelled post.
            return SyncResult(**base, action="skipped", status="recovering", post_id=post["id"], message=(
                f"The last publish to {platform.name} was interrupted, and {platform.name} hasn't said yet whether the post "
                f"arrived; Inkcast asks again every few minutes, for up to INKCAST_PUBLISH_RECOVERY_MAX_HOURS, then leaves "
                f"it to you as unconfirmed. Check {platform.name}, then cancel post {post['id']} (inkcast_cancel, or "
                f"POST /api/v1/posts/{post['id']}/cancel) if you don't want it: the next sync then publishes this version "
                f"as a new post, so remove the first copy on {platform.name} if it did arrive."))

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

    async def _edit_waiting(self, user_id: str, req: SyncRequest, path: str, platform: Platform, prepared: Prepared,
                            digest: str, post: dict, story) -> SyncResult:
        """A changed file whose post the scheduler has tried and that waits for its retry (#204, ADR-0017): the post is
        changed in place, never replaced. A replacement was a new post, untried and due at the file's time, which has
        passed: it went out at the next tick, before the platform's Retry-After, and its attempts started again.
        In place, the post keeps its attempts and the platform's wait (``PostsRepo.edit_waiting``) and takes the new
        content and time; the file's entry takes the new content in the same transaction."""
        src, pid = req.source, platform.id
        base = {"path": path, "platform": pid}
        wanted = story.publish_at.timestamp() if story.publish_at else None
        if req.dry_run:
            return SyncResult(**base, action="update", status="scheduled", post_id=post["id"], preview=prepared.preview,
                              message="Would change the post that waits for its retry, keeping the retry.")
        refused = await self.publisher.refusal(user_id, story, platform, prepared, retry=True)
        if refused is not None:  # the waiting post and the entry stay as they are; the next sync tries again
            return SyncResult(**base, action=refused.status, status="scheduled", post_id=post["id"], message=refused.message)

        def record(c: sqlite3.Connection) -> None:
            self.publisher.store.put_entry_in(c, user_id, src, path, pid, content_hash=digest, status="scheduled",
                                              post_id=post["id"], message=None)

        run_at = self.publisher.store.edit_waiting(post["id"], user_id, attempts=post["attempts"], title=prepared.title,
                                                   payload=prepared.to_dict(), requested_at=wanted, also=record)
        if run_at is None:  # the scheduler claimed it, or tried it again, since this sync read it
            return SyncResult(**base, action="skipped", status="scheduled", post_id=post["id"],
                              message="The scheduled post changed state during this sync; run the sync again.")
        when = datetime.fromtimestamp(run_at, timezone.utc).isoformat(timespec="seconds")
        waits = run_at - (time.time() if wanted is None else wanted) >= 1
        return SyncResult(**base, action="update", status="scheduled", post_id=post["id"], message=(
            f"It waits for the retry of its last send to {platform.name}: it goes out at {when}." if waits else None))

    async def _create(self, user_id: str, req: SyncRequest, path: str, story, platform: Platform, prepared: Prepared,
                      digest: str, entry: dict | None, replace: bool = False,
                      created_at: float | None = None) -> SyncResult:
        src, pid = req.source, platform.id
        base = {"path": path, "platform": pid}
        action = "update" if replace else "create"
        if req.dry_run:
            return SyncResult(**base, action=action, status="scheduled" if story.publish_at else "preview",
                              preview=prepared.preview)
        if not replace and not self.store.claim_new(user_id, src, path, pid, digest):
            return SyncResult(**base, action="skipped", message="Another sync of this file is in progress.")

        made: dict[str, str] = {}  # the post row this sync made, known before the write that names it (below)
        said: str | None = None  # why the platform refused the post, kept if the write of that outcome fails (#229)

        # Before the send, so a crash mid-send can't orphan the post; in the row's own transaction, so a failed write
        # leaves no row that nothing names, for recovery to send while the next sync makes another.
        def on_record(_pid: str, post_id: str) -> None:
            made["post_id"] = post_id
            # Through the publisher's store: the write runs inside create_post's transaction, on that connection
            # (a second connection would wait on the lock the first holds). In production the two are one store.
            # With the content it carries: if every later write of the entry fails, or the process stops, the entry
            # that names the replacement must not keep the replaced post's hash, or the next sync takes the unchanged
            # file for an edit and replaces the replacement, or makes again one a person cancelled (#229).
            self.publisher.store.put_entry(user_id, src, path, pid, post_id=post_id, content_hash=digest)

        try:
            # retry: a "now" post the platform rate-limits waits for the scheduler's retry (and the 429's
            # Retry-After) instead of failing, which the next CI run would create afresh, paced by nothing (#188).
            [o] = await self.publisher.publish(user_id, story, [pid], on_created=on_record, created_at=created_at,
                                               retry=True)
            if o.status in ("failed", "skipped"):
                said = o.message
                if replace:
                    self.store.put_entry(user_id, src, path, pid, status="failed", message=o.message)
                else:
                    self.store.put_entry(user_id, src, path, pid, status="failed", content_hash=digest, message=o.message)
                return SyncResult(**base, action="failed" if o.status == "failed" else "skipped", message=o.message)
            self.store.put_entry(user_id, src, path, pid, content_hash=digest, post_id=o.post_id, status=o.status,
                                 url=o.url, remote_id=o.remote_id,
                                 message=o.message if o.status in ("unconfirmed", "scheduled") else None)
        except Exception:
            self._after_error(user_id, src, path, pid, digest, made.get("post_id"), said)
            raise
        return SyncResult(**base, action=action, status=o.status, post_id=o.post_id, url=o.url,
                          message=o.message or ("; ".join(o.notes) or None))

    def _removed(self, user_id: str, req: SyncRequest, path: str) -> list[SyncResult]:
        """A file removed from the repository (#230): one result per platform entry of (user, source, path). A pending
        post ('scheduled', 'recovering' or 'unconfirmed') is cancelled through the transition a person's cancel uses
        (``Publisher.cancel_pending``), with the entry's write in that transaction. The cancel is one conditional write,
        so a sync of the same path racing this one (an edit's replace, the scheduler's claim, a person's cancel) can't
        both move the post: the loser reports the post changed state and asks for a re-run. A published post stays on
        the platform: Inkcast never deletes there. The entry stays, pointing at the cancelled post. A scheduled post was
        never sent, so nothing stands as sent (an empty hash) and a file put back at the same path is published as a new
        post; one that may already be on the platform keeps its hash, so only an edited file is, as after a person's
        cancel. Only this user's entries of this repository are read: a removal never touches
        another person's post, nor a post of the same path from another repository."""
        entries = self.store.list_entries(user_id, req.source, path)
        if not entries:
            return [SyncResult(path=path, platform="*", action="removed",
                               message="No post came from this file; nothing to cancel.")]
        return [self._cancel_removed(user_id, req, path, entry) for entry in entries]

    def _cancel_removed(self, user_id: str, req: SyncRequest, path: str, entry: dict) -> SyncResult:
        src, pid = req.source, entry["platform"]
        name = getattr(self.platforms.get(pid), "name", pid)
        base = {"path": path, "platform": pid}
        if entry["status"] == "syncing" and time.time() - entry["synced_at"] < CLAIM_TTL:
            # Its post is being made now. The claim names it before the sync holding it writes the entry's status: a
            # cancel in between would be recorded 'scheduled' by that write, and never followed once the file is gone.
            return SyncResult(**base, action="skipped", status="syncing",
                              message="Another sync of this file is in progress; run the sync again.")
        post = self.store.get_post(entry["post_id"]) if entry["post_id"] else None
        if post is None:
            if entry["status"] == "unconfirmed":  # from before posts were recorded: nothing to cancel, maybe published
                return SyncResult(**base, action="removed", status="unconfirmed", message=(
                    f"Its last publish to {name} may or may not have gone through, and Inkcast has no post of it to "
                    f"cancel. Check {name}, and remove it there if it did arrive."))
            return SyncResult(**base, action="removed", status=entry["status"],
                              message=f"Inkcast has no post of this file on {name}; nothing to cancel.")
        found = {"status": post["status"], "post_id": post["id"], "url": entry["url"] or post["result_url"]}
        if post["status"] == "publishing":
            return SyncResult(**base, action="skipped", **found, message=(
                f"Its post is being sent to {name} right now and can't be cancelled; run the sync again once it has "
                f"settled, or cancel it then (inkcast_cancel, or POST /api/v1/posts/{post['id']}/cancel)."))
        if post["status"] not in self.store.PENDING:
            if post["status"] in ("published", "draft", "unlisted"):
                return SyncResult(**base, action="removed", **found, message=(
                    f"Post {post['id']} is {post['status']} and stays on {name}: Inkcast never removes a post from a "
                    f"platform. Remove it there if you don't want it."))
            return SyncResult(**base, action="removed", **found, message=f"Post {post['id']} is {post['status']}; nothing to cancel.")
        maybe = "" if post["status"] == "scheduled" else f" It may already be on {name}: check, and remove it there if it is."
        if req.dry_run:
            return SyncResult(**base, action="removed", **found, message=f"Would cancel post {post['id']} ({post['status']}).{maybe}")

        # Only a scheduled post was never sent: put back, its file is published anew. One that may already be on the
        # platform keeps its hash, so the same file put back is 'unchanged', as after a person's cancel: no second copy.
        cleared = {"content_hash": ""} if post["status"] == "scheduled" else {}

        def record(c: sqlite3.Connection) -> None:
            self.publisher.store.put_entry_in(c, user_id, src, path, pid, status="cancelled", **cleared,
                                              message="Removed from the repository; its post was cancelled.")

        try:
            self.publisher.cancel_pending(user_id, post["id"], also=record)
        except PlatformError:  # it went out, the scheduler claimed it, a person decided, or another sync replaced it
            now = self.store.get_post(post["id"]) or post
            return SyncResult(**base, action="skipped", status=now["status"], post_id=post["id"],
                              message="The post changed state during this sync; run the sync again.")
        return SyncResult(**base, action="removed", status="cancelled", post_id=post["id"], url=entry["url"],
                          message=f"Post {post['id']} was cancelled.{maybe}")

    @staticmethod
    def _decision(platform: Platform, post_id: str, checked: Outcome | None, dry_run: bool = False) -> str:
        """What the sync says of a file whose post is unconfirmed: the person decides (#226)."""
        said = (f"this dry run didn't ask {platform.name}" if dry_run
                else f"{platform.name} can't be checked automatically" if checked is None
                else f"{platform.name} couldn't be asked just now ({checked.message}); the next sync asks again"
                if checked.status == "unavailable" else f"{platform.name} says it isn't there")
        return (f"The last publish to {platform.name} may or may not have gone through, and {said}. Inkcast never publishes "
                f"it again on its own: check {platform.name}, then resend post {post_id} (inkcast_resend, or POST "
                f"/api/v1/posts/{post_id}/resend) to send it as it was, or cancel it (inkcast_cancel, or POST "
                f"/api/v1/posts/{post_id}/cancel) if you don't want it. After a cancel, an edited file is published as a "
                f"new post, so remove the first copy on {platform.name} if it did arrive.")

    def _after_error(self, user_id: str, src: str, path: str, pid: str, digest: str, post_id: str | None,
                     said: str | None = None) -> None:
        """The publish, or the ledger write of its outcome, raised (a locked database, say). Once the post row exists
        the entry points at it, with the post's own status, so the next sync follows the post and never creates it
        again: 'publishing' is the scheduler's to recover (asked, never re-sent), 'scheduled' waits for its time,
        'unconfirmed' is checked (#213). Only an error before any post row exists marks the entry 'failed', which
        the next sync creates afresh; so too on the replace path, whose claim names no post until its own exists.
        ``said``: the platform's reason for refusing the post, when the error came in the write of that outcome: the
        entry keeps it, not "internal error" (#229)."""
        post = self.store.get_post(post_id) if post_id else None
        if post is None:
            self.store.put_entry(user_id, src, path, pid, status="failed", message=said or "internal error")
            return
        self.store.put_entry(user_id, src, path, pid, content_hash=digest, post_id=post["id"], status=post["status"],
                             url=post["result_url"], remote_id=post["remote_id"],
                             message=said or "Internal error after the post was made; its status says what became of it.")

    def _resolve(self, user_id: str, src: str, path: str, platform: Platform, entry: dict, o: Outcome | None) -> dict | None:
        """Resolve an unconfirmed entry from what the platform said (``Publisher.confirm``): published → record it;
        missing → allow a fresh create (an entry with no post; one with a post is the person's, ``_one``); None →
        can't tell. The post is written only while it is still 'unconfirmed': a person may have cancelled or resent it
        while the lookup was out, and then the entry follows what they decided (#226)."""
        if o is None:
            return None
        if o.status == "missing":
            if entry["post_id"]:
                self.store.update_post(entry["post_id"], status="failed", only_if="unconfirmed",
                                       error=f"Not found on {platform.name}; it was never published.")
            return self.store.put_entry(user_id, src, path, platform.id, status="missing", message=None)
        if entry["post_id"] and not self.store.update_post(entry["post_id"], status=o.status, result_url=o.url,
                                                           remote_id=o.remote_id, error=None, only_if="unconfirmed"):
            # Decided meanwhile: a resend re-pointed the entry at its new post; a cancel left it on this one.
            current = self.store.get_entry(user_id, src, path, platform.id) or entry
            post = self.store.get_post(current["post_id"]) if current["post_id"] else None
            if post is not None and post["status"] != current["status"]:
                return self.store.put_entry(user_id, src, path, platform.id, status=post["status"], url=post["result_url"],
                                            remote_id=post["remote_id"], message=None)
            return current
        return self.store.put_entry(user_id, src, path, platform.id, status=o.status, url=o.url, remote_id=o.remote_id,
                                    message=None)

    def ledger(self, user_id: str, source: str | None = None) -> list[LedgerEntry]:
        return [LedgerEntry(source=r["source"], path=r["path"], platform=r["platform"], status=r["status"],
                            content_hash=r["content_hash"], post_id=r["post_id"], url=r["url"], message=r["message"],
                            synced_at=datetime.fromtimestamp(r["synced_at"], timezone.utc).isoformat(timespec="seconds"))
                for r in self.store.list_entries(user_id, source)]
