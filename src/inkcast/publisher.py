# SPDX-License-Identifier: PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Cross-posting and scheduling.

A publish request becomes one post row per platform (sharing a group id).
"Now" posts are sent immediately; scheduled posts wait in the database and
the scheduler loop sends them when due, retrying transient failures.
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
from .storage import Store
from .story import Story

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
RETRY_DELAYS = [60, 300, 1800]  # seconds after attempt 1, 2, 3


@dataclass
class Outcome:
    platform: str
    status: str  # published | draft | scheduled | failed | skipped | preview
    post_id: str | None = None
    url: str | None = None
    message: str | None = None
    prepared: Prepared | None = None
    notes: list[str] = field(default_factory=list)


class Publisher:
    def __init__(self, store: Store, platforms: dict[str, Platform], metrics: Any = None):
        self.store = store
        self.platforms = platforms
        self.metrics = metrics
        self.last_tick: float = 0.0  # for readiness: is the scheduler loop alive?

    def _count(self, platform: str, status: str) -> None:
        if self.metrics:
            self.metrics.inc("inkcast_publish_total", platform=platform, status=status)

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

    async def publish(self, user_id: str, story: Story, platform_ids: list[str]) -> list[Outcome]:
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
            if story.publish_at:
                outcomes.append(Outcome(platform.id, "scheduled", post_id=post_id, prepared=prepared))
            else:
                outcomes.append(await self._send(post_id, user_id, platform, prepared, attempts=1))
        order = {pid: i for i, pid in enumerate(platform_ids)}
        return sorted(outcomes, key=lambda o: order.get(o.platform, 99))

    async def _send(self, post_id: str, user_id: str, platform: Platform, prepared: Prepared, attempts: int,
                    scheduled: bool = False) -> Outcome:
        connected = self.store.get_credentials(user_id, platform.id)
        if not connected:
            self.store.update_post(post_id, status="failed", error=f"{platform.name} was disconnected.")
            return Outcome(platform.id, "failed", post_id=post_id, message=f"{platform.name} was disconnected.")
        creds, account_data = connected
        account = Account.from_dict(account_data)
        try:
            destination = (
                await platform.resolve_destination(creds, account, prepared.destination) if prepared.destination else None
            )
            result = await platform.publish(creds, account, prepared, destination)
        except PlatformError as exc:
            if exc.uncertain:
                self.store.update_post(post_id, status="unknown", error=str(exc))
                self._count(platform.id, "unknown")
                return Outcome(platform.id, "unknown", post_id=post_id, message=str(exc))
            if scheduled and exc.retryable and attempts < MAX_ATTEMPTS:
                delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
                self.store.update_post(post_id, status="scheduled", run_at=time.time() + delay, error=f"retrying: {exc}")
                self._count(platform.id, "retry")
                return Outcome(platform.id, "scheduled", post_id=post_id, message=f"Will retry: {exc}")
            self.store.update_post(post_id, status="failed", error=str(exc))
            self._count(platform.id, "failed")
            return Outcome(platform.id, "failed", post_id=post_id, message=str(exc))
        except Exception as exc:  # a plug-in bug must not kill the scheduler
            log.exception("Platform %s crashed publishing %s", platform.id, post_id)
            self.store.update_post(post_id, status="failed", error=f"internal error: {exc}")
            self._count(platform.id, "failed")
            return Outcome(platform.id, "failed", post_id=post_id, message=f"Internal error in the {platform.name} plug-in.")

        status = result.status if result.status in ("draft", "unlisted") else "published"
        self.store.update_post(post_id, status=status, result_url=result.url, remote_id=result.remote_id,
                               error="; ".join(result.notes) or None)
        self._count(platform.id, status)
        return Outcome(platform.id, status, post_id=post_id, url=result.url, notes=result.notes, prepared=prepared)

    async def run_due(self, now: float | None = None) -> list[Outcome]:
        now = now or time.time()
        self.store.recover_stuck(older_than=now - 600)
        outcomes = []
        for row in self.store.claim_due_posts(now):
            platform = self.platforms.get(row["platform"])
            if platform is None:
                self.store.update_post(row["id"], status="failed", error=f"Platform plug-in {row['platform']!r} is not installed.")
                continue
            prepared = Prepared.from_dict(json.loads(row["payload"]))
            outcomes.append(await self._send(row["id"], row["user_id"], platform, prepared, row["attempts"], scheduled=True))
        return outcomes

    async def run_forever(self, interval: float, stop: asyncio.Event, housekeeping: Any = None) -> None:
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
                if self.metrics:
                    self.metrics.inc("inkcast_scheduler_ticks_total")
                if housekeeping and ticks % 90 == 0:
                    housekeeping()
            except Exception:
                log.exception("Scheduler tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        log.info("Scheduler stopped")

    def cancel(self, user_id: str, post_id: str) -> str:
        row = self.store.get_post(post_id)
        if not row or row["user_id"] != user_id:
            raise PlatformError(f"No post {post_id!r}.")
        if row["status"] != "scheduled":
            raise PlatformError(
                f"Post {post_id} is {row['status']}; only scheduled posts can be cancelled. Published stories have to "
                "be removed on the platform itself."
            )
        self.store.update_post(post_id, status="cancelled")
        return row["title"]
