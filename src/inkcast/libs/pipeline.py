# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Cross-posting and scheduling.

A publish request becomes one post row per platform (sharing a group id).
"Now" posts are sent immediately; scheduled posts wait in the database and
the scheduler loop sends them when due, retrying transient failures under the
``RetryPolicy`` the Publisher was built with (INKCAST_PUBLISH_MAX_ATTEMPTS and
INKCAST_PUBLISH_RETRY_DELAYS; libs/config). Only a failure whose send certainly
never happened is retried: an uncertain one is ``unconfirmed``, whatever the policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .platforms import Account, Platform, PlatformError, Prepared
from .config import RetryPolicy
from .db import Store
from .content.story import Story
from .events import DomainEvent

log = logging.getLogger(__name__)

STUCK_AFTER = 600  # a post 'publishing' for longer than this was interrupted (the HTTP timeout is 30 s)


@dataclass
class Outcome:
    platform: str
    status: str  # published | draft | scheduled | failed | skipped | preview
    post_id: str | None = None
    url: str | None = None
    message: str | None = None
    prepared: Prepared | None = None
    notes: list[str] = field(default_factory=list)
    remote_id: str | None = None


class Publisher:
    def __init__(self, store: Store, platforms: dict[str, Platform], metrics: Any = None,
                 retry: RetryPolicy = RetryPolicy()):
        self.store = store
        self.platforms = platforms
        self.metrics = metrics
        self.retry = retry  # the scheduler's retry policy (config.publish_retry); the default is 4 sends over ~36 min
        self.last_tick: float = 0.0  # for readiness: is the scheduler loop alive?

    def _count(self, platform: str, status: str) -> None:
        if self.metrics:
            self.metrics.inc("inkcast_publish_total", platform=platform, status=status)

    def _observe(self, name: str, value: float, **labels: str) -> None:
        if self.metrics:
            self.metrics.observe(name, value, **labels)

    @staticmethod
    def _event(type: str, post_id: str, user_id: str, platform: str, **attrs: Any) -> list[DomainEvent]:
        """The domain event written with a post's status change (libs/events; the outbox)."""
        return [DomainEvent(type, user_id=user_id, subject=post_id, attrs={"platform": platform, **attrs})]

    def prepare(self, story: Story, platform_ids: list[str]) -> tuple[list[Prepared], list[Outcome]]:
        """Render for each platform; validation problems become 'failed' outcomes, nothing is sent."""
        ready: list[Prepared] = []
        problems: list[Outcome] = []
        for pid in platform_ids:
            try:
                ready.append(self.platforms[pid].prepare(story.for_platform(pid), story.body))
            except PlatformError as exc:
                problems.append(Outcome(pid, "failed", message=str(exc)))
        return ready, problems

    async def publish(self, user_id: str, story: Story, platform_ids: list[str],
                      on_created: Any = None) -> list[Outcome]:
        """``on_created(platform_id, post_id)`` runs once each post row exists and before anything is sent."""
        ready, outcomes = self.prepare(story, platform_ids)
        group = "grp_" + uuid.uuid4().hex[:10]
        for prepared in ready:
            platform = self.platforms[prepared.platform]
            connected = self.store.get_credentials(user_id, platform.id)
            if not connected:
                help_text = platform.not_connected_help(story.for_platform(platform.id))
                outcomes.append(Outcome(platform.id, "skipped", message=(
                    f"{platform.name} isn't connected. Use inkcast_connect to add it."
                    + (f" {help_text}" if help_text else "")
                )))
                continue
            creds, account = connected
            # Resolve destinations up front so a typo fails now, not at 3am.
            try:
                if prepared.destination:
                    await platform.resolve_destination(creds, Account.from_dict(account), prepared.destination)
            except PlatformError as exc:
                outcomes.append(Outcome(platform.id, "failed", message=str(exc)))
                continue

            run_at = story.publish_at.timestamp() if story.publish_at else time.time()
            post_id = self.store.create_post(
                group_id=group, user_id=user_id, platform=platform.id, title=prepared.title,
                status="scheduled" if story.publish_at else "publishing", run_at=run_at, payload=prepared.to_dict(),
            )
            if on_created:
                on_created(platform.id, post_id)
            if story.publish_at:
                outcomes.append(Outcome(platform.id, "scheduled", post_id=post_id, prepared=prepared))
            else:
                outcomes.append(await self._send(post_id, user_id, platform, prepared, attempts=1))
        order = {pid: i for i, pid in enumerate(platform_ids)}
        return sorted(outcomes, key=lambda o: order.get(o.platform, 99))

    async def _send(self, post_id: str, user_id: str, platform: Platform, prepared: Prepared, attempts: int,
                    scheduled: bool = False, lag: float | None = None) -> Outcome:
        connected = self.store.get_credentials(user_id, platform.id)
        if not connected:
            self.store.update_post(post_id, status="failed", error=f"{platform.name} was disconnected.",
                                   events=self._event("post.failed", post_id, user_id, platform.id, reason="disconnected"))
            return Outcome(platform.id, "failed", post_id=post_id, message=f"{platform.name} was disconnected.")
        creds, account_data = connected
        account = Account.from_dict(account_data)
        started = time.perf_counter()
        try:
            destination = (
                await platform.resolve_destination(creds, account, prepared.destination) if prepared.destination else None
            )
            result = await platform.publish(creds, account, prepared, destination)
        except PlatformError as exc:
            self._observe("inkcast_publish_duration_seconds", time.perf_counter() - started, platform=platform.id)
            if exc.uncertain:
                # Never retried, under any policy: the post may already be out, and a retry could post it twice.
                self.store.update_post(post_id, status="unconfirmed", error=str(exc),
                                       events=self._event("post.unconfirmed", post_id, user_id, platform.id))
                self._count(platform.id, "unconfirmed")
                log.warning("Post %s on %s is unconfirmed: %s", post_id, platform.id, exc,
                            extra={"event": "publish.unconfirmed", "post_id": post_id, "platform": platform.id})
                return Outcome(platform.id, "unconfirmed", post_id=post_id, message=str(exc))
            if scheduled and exc.retryable and attempts < self.retry.max_attempts:
                delay = self.retry.delay_after(attempts)
                self.store.update_post(post_id, status="scheduled", run_at=time.time() + delay, error=f"retrying: {exc}",
                                       events=self._event("post.scheduled", post_id, user_id, platform.id, retry=attempts))
                self._count(platform.id, "retry")
                return Outcome(platform.id, "scheduled", post_id=post_id, message=f"Will retry: {exc}")
            self.store.update_post(post_id, status="failed", error=str(exc),
                                   events=self._event("post.failed", post_id, user_id, platform.id, reason="platform"))
            self._count(platform.id, "failed")
            return Outcome(platform.id, "failed", post_id=post_id, message=str(exc))
        except Exception as exc:  # a plug-in bug must not kill the scheduler
            # It may have crashed after the platform accepted the post (reading the reply), so it is ambiguous like
            # any uncertain failure: unconfirmed, never 'failed', which Git sync would create again.
            log.exception("Platform %s crashed publishing %s", platform.id, post_id)
            self.store.update_post(post_id, status="unconfirmed", error=f"internal error: {exc}",
                                   events=self._event("post.unconfirmed", post_id, user_id, platform.id))
            self._count(platform.id, "unconfirmed")
            return Outcome(platform.id, "unconfirmed", post_id=post_id, message=(
                f"Internal error in the {platform.name} plug-in; the post may or may not have been published. "
                f"Check {platform.name} before trying again."))

        self._observe("inkcast_publish_duration_seconds", time.perf_counter() - started, platform=platform.id)
        status = result.status if result.status in ("draft", "unlisted") else "published"
        self.store.update_post(post_id, status=status, result_url=result.url, remote_id=result.remote_id,
                               error="; ".join(result.notes) or None,
                               events=self._event("post.published", post_id, user_id, platform.id, status=status,
                                                  scheduled=scheduled, attempts=attempts,
                                                  lag_seconds=round(lag, 3) if lag is not None else None))
        self._count(platform.id, status)
        return Outcome(platform.id, status, post_id=post_id, url=result.url, notes=result.notes, prepared=prepared,
                       remote_id=result.remote_id)

    async def update(self, user_id: str, post_id: str | None, platform: Platform, prepared: Prepared,
                     remote_id: str) -> Outcome:
        """Edit a published post in place (Git publishing). A PUT is safe to repeat, so an ambiguous
        failure is reported as failed and the next sync simply tries again."""
        connected = self.store.get_credentials(user_id, platform.id)
        if not connected:
            return Outcome(platform.id, "skipped", post_id=post_id, message=f"{platform.name} isn't connected.")
        creds, account_data = connected
        account = Account.from_dict(account_data)
        try:
            destination = (
                await platform.resolve_destination(creds, account, prepared.destination) if prepared.destination else None
            )
            result = await platform.update(creds, account, remote_id, prepared, destination)
        except PlatformError as exc:
            self._count(platform.id, "update_failed")
            return Outcome(platform.id, "failed", post_id=post_id, message=str(exc), prepared=prepared)
        except Exception as exc:  # a plug-in bug must not break the sync
            log.exception("Platform %s crashed updating %s", platform.id, remote_id)
            return Outcome(platform.id, "failed", post_id=post_id, message=f"Internal error in the {platform.name} "
                           f"plug-in: {exc}", prepared=prepared)
        status = result.status if result.status in ("draft", "unlisted") else "published"
        if post_id:
            self.store.update_post(post_id, status=status, title=prepared.title, payload=json.dumps(prepared.to_dict()),
                                   result_url=result.url or None, error="; ".join(result.notes) or None)
        self._count(platform.id, "updated")
        return Outcome(platform.id, status, post_id=post_id, url=result.url, notes=result.notes, prepared=prepared,
                       remote_id=result.remote_id or remote_id)

    async def confirm(self, user_id: str, platform: Platform, prepared: Prepared) -> Outcome | None:
        """For an ``unconfirmed`` publish: ask the platform whether the post exists. None = can't tell."""
        if "find" not in platform.capabilities:
            return None
        connected = self.store.get_credentials(user_id, platform.id)
        if not connected:
            return None
        creds, account_data = connected
        try:
            found = await platform.find_existing(creds, Account.from_dict(account_data), prepared)
        except PlatformError:
            return None
        except Exception:  # a plug-in bug: can't tell, and it must not stop the other posts being resolved
            log.exception("Platform %s crashed looking up an existing post", platform.id)
            return None
        if found is None:
            return Outcome(platform.id, "missing")
        status = found.status if found.status in ("draft", "unlisted") else "published"
        return Outcome(platform.id, status, url=found.url, remote_id=found.remote_id)

    async def recover(self, row: dict[str, Any]) -> Outcome:
        """A post the process was sending when it stopped. The request may or may not have reached the platform,
        so: ask the platform (``find_existing``) when it can tell; otherwise mark it ``unconfirmed`` for a person.
        Only a post the platform says is *missing* goes back in the queue."""
        post_id, platform = row["id"], self.platforms.get(row["platform"])
        if platform is None:
            self.store.update_post(post_id, status="failed", error=f"Platform plug-in {row['platform']!r} is not installed.",
                                   events=self._event("post.failed", post_id, row["user_id"], row["platform"], reason="plugin"))
            return Outcome(row["platform"], "failed", post_id=post_id)
        prepared = Prepared.from_dict(json.loads(row["payload"]))
        found = await self.confirm(row["user_id"], platform, prepared)

        def recovered(status: str) -> list[DomainEvent]:
            return self._event("post.recovered", post_id, row["user_id"], platform.id, status=status)

        if found is None:
            message = (f"Inkcast restarted while this post was being sent to {platform.name}, which can't be checked "
                       f"automatically. Check {platform.name} before publishing it again.")
            self.store.update_post(post_id, status="unconfirmed", error=message, events=recovered("unconfirmed"))
            outcome = Outcome(platform.id, "unconfirmed", post_id=post_id, message=message)
        elif found.status == "missing":
            self.store.update_post(post_id, status="scheduled", run_at=time.time(), events=recovered("scheduled"),
                                   error=f"Interrupted before {platform.name} received it; sending again.")
            outcome = Outcome(platform.id, "scheduled", post_id=post_id, message="Interrupted; sending again.")
        else:
            self.store.update_post(post_id, status=found.status, result_url=found.url, remote_id=found.remote_id, error=None,
                                   events=recovered(found.status))
            outcome = Outcome(platform.id, found.status, post_id=post_id, url=found.url, remote_id=found.remote_id)
        self._count(platform.id, "recovered")
        log.warning("Post %s on %s was interrupted mid-send; now %s", post_id, platform.id, outcome.status,
                    extra={"event": "publish.recovered", "post_id": post_id, "platform": platform.id, "status": outcome.status})
        return outcome

    async def run_due(self, now: float | None = None) -> list[Outcome]:
        now = now or time.time()
        outcomes = [await self.recover(row) for row in self.store.claim_stuck(older_than=now - STUCK_AFTER)]
        for row in self.store.claim_due_posts(now):
            if not self.store.renew_claim(row["id"], row["attempts"]):
                continue  # another worker took it over as stuck while this batch was being sent: it is theirs now
            lag = max(0.0, now - float(row["run_at"]))
            self._observe("inkcast_publish_lag_seconds", lag, platform=row["platform"])
            platform = self.platforms.get(row["platform"])
            if platform is None:
                self.store.update_post(row["id"], status="failed", error=f"Platform plug-in {row['platform']!r} is not installed.",
                                       events=self._event("post.failed", row["id"], row["user_id"], row["platform"], reason="plugin"))
                continue
            prepared = Prepared.from_dict(json.loads(row["payload"]))
            outcomes.append(await self._send(row["id"], row["user_id"], platform, prepared, row["attempts"], scheduled=True,
                                             lag=lag))
        return outcomes

    async def run_forever(self, interval: float, stop: asyncio.Event, housekeeping: Any = None,
                          on_tick: Any = None) -> None:
        """Tick until ``stop`` is set. A tick in progress always finishes (no half-sent posts on SIGTERM)."""
        log.info("Scheduler running every %.0fs", interval)
        ticks = 0
        while not stop.is_set():
            try:
                for o in await self.run_due():
                    log.info("Scheduled post %s on %s: %s %s", o.post_id, o.platform, o.status, o.url or o.message or "",
                             extra={"post_id": o.post_id, "platform": o.platform, "status": o.status})
                ticks += 1
                self.last_tick = time.time()
                if on_tick:
                    on_tick()
                if self.metrics:
                    self.metrics.inc("inkcast_scheduler_ticks_total")
                if housekeeping and ticks % 90 == 0:
                    housekeeping()
            except Exception:
                log.exception("Scheduler tick failed", extra={"event": "scheduler.tick.failed"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        log.info("Scheduler stopped")

    def cancel(self, user_id: str, post_id: str) -> str:
        row = self.store.get_post(post_id)
        if not row or row["user_id"] != user_id:
            raise PlatformError(f"No post {post_id!r}.")
        # One conditional update, so a scheduler claim can't land between a status check and the write.
        if not self.store.cancel_if_scheduled(
                post_id, user_id, events=self._event("post.cancelled", post_id, user_id, row["platform"])):
            status = (self.store.get_post(post_id) or row)["status"]
            raise PlatformError(
                f"Post {post_id} is {status}; only scheduled posts can be cancelled. Published stories have to "
                "be removed on the platform itself."
            )
        return row["title"]
