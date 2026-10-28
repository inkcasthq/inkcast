# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Cross-posting and scheduling.

A publish request becomes one post row per platform (sharing a group id).
"Now" posts are sent immediately; scheduled posts wait in the database and
the scheduler loop sends them when due, retrying transient failures under the
``RetryPolicy`` the Publisher was built with (INKCAST_PUBLISH_MAX_ATTEMPTS and
INKCAST_PUBLISH_RETRY_DELAYS; libs/config). A 429's ``Retry-After`` lengthens
the wait, up to the policy's largest delay. Only a failure whose send certainly
never happened is retried: an uncertain one is ``unconfirmed``, whatever the policy.
A send the process was interrupted in is ``recovering`` until the platform says whether
it arrived, for at most INKCAST_PUBLISH_RECOVERY_MAX_HOURS; a person can cancel it meanwhile. An ``unconfirmed``
post is a person's to cancel or resend (``resend``: one new post); it is never sent again on its own.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .platforms import Account, Platform, PlatformError, Prepared
from .config import RetryPolicy
from .db import Store
from .content.story import Story
from .events import DomainEvent

log = logging.getLogger(__name__)

STUCK_AFTER = 600  # a post 'publishing' for longer than this was interrupted (the HTTP timeout is 30 s)
RECOVERY_MAX = 24 * 3600  # seconds a post stays 'recovering' before it is unconfirmed: INKCAST_PUBLISH_RECOVERY_MAX_HOURS's default


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
                 retry: RetryPolicy = RetryPolicy(), recovery_max: float = RECOVERY_MAX):
        self.store = store
        self.platforms = platforms
        self.metrics = metrics
        self.retry = retry  # the scheduler's retry policy (config.publish_retry); the default is 4 sends over ~36 min
        # Seconds a post whose send was interrupted is checked with the platform before it is unconfirmed for a person
        # (config.publish_recovery_max_seconds, INKCAST_PUBLISH_RECOVERY_MAX_HOURS; #216): never sent late on its own.
        self.recovery_max = recovery_max
        self.last_tick: float = 0.0  # for readiness: is the scheduler loop alive?

    def _count(self, platform: str, status: str) -> None:
        if self.metrics:
            self.metrics.inc("inkcast_publish_total", platform=platform, status=status)

    def _observe(self, name: str, value: float, **labels: str) -> None:
        if self.metrics:
            self.metrics.observe(name, value, **labels)

    def _asked_beyond_cap(self, asked: Any) -> bool:
        """Whether a platform's Retry-After asked for longer than the policy's largest delay, the cap on any wait."""
        try:
            return math.ceil(asked) > max(self.retry.delays)
        except (TypeError, ValueError, OverflowError):  # None or a string; nan; inf
            return False

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
                      on_created: Any = None, created_at: float | None = None, retry: bool = False) -> list[Outcome]:
        """``on_created(platform_id, post_id)`` runs in the transaction that makes each post row, before anything is
        sent: if it raises, the row is not made (``PostsRepo.create_post``).
        ``created_at``: for a replacement that keeps the schedule of the post it replaces (``PostsRepo.create_post``).
        ``retry``: a "now" post whose send certainly never happened (a 429, say) is handed to the scheduler, which
        retries it under the policy, instead of failing at once. Git sync asks for it (there is nobody reading the
        reply to try again later, #188); a person's MCP or REST publish is told what happened and decides."""
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
                # A lookup that will pass (a 429) is no typo: when asked to retry, the send resolves it again, and
                # its retryable failure goes to the scheduler under the policy and the Retry-After (#188).
                if not (retry and getattr(exc, "retryable", False)):
                    outcomes.append(Outcome(platform.id, "failed", message=str(exc)))
                    continue

            run_at = story.publish_at.timestamp() if story.publish_at else time.time()
            post_id = self.store.create_post(
                group_id=group, user_id=user_id, platform=platform.id, title=prepared.title,
                status="scheduled" if story.publish_at else "publishing", run_at=run_at, payload=prepared.to_dict(),
                created_at=created_at, on_created=functools.partial(on_created, platform.id) if on_created else None,
            )
            if story.publish_at:
                outcomes.append(Outcome(platform.id, "scheduled", post_id=post_id, prepared=prepared))
            else:
                outcomes.append(await self._send(post_id, user_id, platform, prepared, attempts=1, retry=retry, held=0))
        order = {pid: i for i, pid in enumerate(platform_ids)}
        return sorted(outcomes, key=lambda o: order.get(o.platform, 99))

    async def _send(self, post_id: str, user_id: str, platform: Platform, prepared: Prepared, attempts: int,
                    scheduled: bool = False, lag: float | None = None, retry: bool = False, held: int = 0) -> Outcome:
        """``scheduled``: sent by the scheduler at its time (recorded on the event). ``retry``: a retryable failure
        is put back in the queue under the policy; always for the scheduler, for a "now" post only when asked.
        ``held``: the row's ``attempts`` while this send holds the post (the scheduler's claim counted its own; a
        "now" post's row still reads 0): the fence of the send's failure writes, like ``renew_claim``'s (#226)."""
        retry = retry or scheduled
        connected = self.store.get_credentials(user_id, platform.id)
        if not connected:
            self.store.update_post(post_id, status="failed", error=f"{platform.name} was disconnected.",
                                   events=self._event("post.failed", post_id, user_id, platform.id, reason="disconnected"))
            return Outcome(platform.id, "failed", post_id=post_id, message=f"{platform.name} was disconnected.")
        creds, account_data = connected
        account = Account.from_dict(account_data)
        try:
            return await self._deliver(post_id, user_id, platform, prepared, attempts, creds, account, scheduled, lag, retry,
                                       held)
        except sqlite3.Error:
            raise  # the store, not the plug-in: it says nothing about the send, so the tick stops and recovery asks
        except Exception as exc:
            # An error the pipeline itself could not handle (a PlatformError subclass missing a flag it reads, an
            # error whose text can't be rendered): one plug-in's odd exception must not leave the rest of the batch
            # claimed until recovery (#174). This post is as ambiguous as a crash inside the plug-in.
            log.exception("Platform %s raised an error the pipeline could not handle for %s", platform.id, post_id)
            return self._crashed(post_id, user_id, platform, exc, held)

    async def _deliver(self, post_id: str, user_id: str, platform: Platform, prepared: Prepared, attempts: int,
                       creds: dict[str, str], account: Account, scheduled: bool, lag: float | None,
                       retry: bool, held: int = 0) -> Outcome:
        """The send itself, from the plug-in's call to the recorded answer. Only this is guarded (``_send``): reading
        the credentials before it says nothing about the send, and failing there must not mark the post unconfirmed."""
        started = time.perf_counter()
        try:
            destination = (
                await platform.resolve_destination(creds, account, prepared.destination) if prepared.destination else None
            )
            result = await platform.publish(creds, account, prepared, destination)
        except PlatformError as exc:
            self._observe("inkcast_publish_duration_seconds", time.perf_counter() - started, platform=platform.id)
            # A send can outlast STUCK_AFTER: another worker then takes the post over as 'recovering', and a person may
            # cancel it (#216). A failure written afterwards must not undo that decision: the cancel stays, and the
            # caller is told what the post is now (``_as_now``), so Git sync's ledger doesn't create it again.
            if exc.uncertain:
                # Never retried, under any policy: the post may already be out, and a retry could post it twice.
                # Not over a cancel (#216), nor over the platform's own word that this very send arrived (another
                # worker's lookup found it, no send since): a post the platform has would read 'unconfirmed' and the
                # person be asked to resend it, a second copy (#226). Over a re-send (a later attempt) it is written:
                # this request may have arrived too, and a person checks for a second copy.
                written = self.store.update_post(post_id, status="unconfirmed", error=str(exc), unless="cancelled",
                                                 unless_found_at=held,
                                                 events=self._event("post.unconfirmed", post_id, user_id, platform.id))
                if written:  # counted, logged and alerted on only as the post's answer, never over a cancel (#226)
                    self._count(platform.id, "unconfirmed")
                    log.warning("Post %s on %s is unconfirmed: %s", post_id, platform.id, exc,
                                extra={"event": "publish.unconfirmed", "post_id": post_id, "platform": platform.id})
                return self._as_now(written, Outcome(platform.id, "unconfirmed", post_id=post_id, message=str(exc)))
            if retry and exc.retryable and attempts < self.retry.max_attempts:
                # A 429's Retry-After, within the policy. getattr: a plug-in's subclass that skips the base constructor
                # (written before the field existed) has none, and must not crash the tick (#174).
                delay = self.retry.delay_after(attempts, asked=getattr(exc, "retry_after", None))
                # attempts: the sends so far. The scheduler's claim counted its own; a "now" post's first send is
                # counted here, so the policy's budget holds and Git sync sees a post the scheduler has tried.
                # Only while the post is still this send's: the request certainly never took effect, so if another
                # worker took the post over meanwhile, its recovery decides (and may have sent it already). "Still
                # this send's" is the status and the attempt count (``held``): that worker's re-send holds the post
                # as 'publishing' too, one attempt on, and a late failure written over it would queue a third (#226).
                written = self.store.update_post(
                    post_id, status="scheduled", run_at=time.time() + delay, attempts=attempts, error=f"retrying: {exc}",
                    only_if="publishing", if_attempts=held,
                    events=self._event("post.scheduled", post_id, user_id, platform.id, retry=attempts))
                if written:
                    self._count(platform.id, "retry")
                return self._as_now(written, Outcome(platform.id, "scheduled", post_id=post_id, message=f"Will retry: {exc}"))
            message = str(exc)
            if retry and exc.retryable and self._asked_beyond_cap(getattr(exc, "retry_after", None)):
                # The retries ran out at the policy's cap while the platform kept asking for longer (#188).
                message += (f" {platform.name} asked for a longer wait than INKCAST_PUBLISH_RETRY_DELAYS allows (at most "
                            f"{max(self.retry.delays)} s), which caps every wait.")
            written = self.store.update_post(post_id, status="failed", error=message, only_if="publishing",  # likewise
                                             if_attempts=held,
                                             events=self._event("post.failed", post_id, user_id, platform.id, reason="platform"))
            if written:
                self._count(platform.id, "failed")
            return self._as_now(written, Outcome(platform.id, "failed", post_id=post_id, message=message))
        except Exception as exc:  # a plug-in bug must not kill the scheduler
            log.exception("Platform %s crashed publishing %s", platform.id, post_id)
            return self._crashed(post_id, user_id, platform, exc, held)

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

    def _crashed(self, post_id: str, user_id: str, platform: Platform, exc: BaseException, held: int = 0) -> Outcome:
        """A plug-in bug while sending. It may have crashed after the platform accepted the post (reading the reply),
        so it is ambiguous like any uncertain failure: unconfirmed, never 'failed', which Git sync would create again."""
        written = self.store.update_post(post_id, status="unconfirmed", error=f"internal error: {exc}", unless="cancelled",
                                         unless_found_at=held,  # as an uncertain answer (_deliver)
                                         events=self._event("post.unconfirmed", post_id, user_id, platform.id))
        if written:
            self._count(platform.id, "unconfirmed")
        return self._as_now(written, Outcome(platform.id, "unconfirmed", post_id=post_id, message=(
            f"Internal error in the {platform.name} plug-in; the post may or may not have been published. "
            f"Check {platform.name} before trying again.")))

    def _as_now(self, written: bool, outcome: Outcome) -> Outcome:
        """A send's answer, as recorded; or, when it was not written because the post moved on while the send was out
        (taken over as stuck and re-sent by another worker, or cancelled: #216), what the post is now, so a caller that
        records the outcome (Git sync's ledger) does not act on an answer the post no longer has. A post that no longer
        exists keeps the send's answer."""
        post = None if written else self.store.get_post(outcome.post_id)
        if post is None:
            return outcome
        return Outcome(outcome.platform, post["status"], post_id=post["id"], url=post["result_url"],
                       remote_id=post["remote_id"], message=post["error"])

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
        """For an ``unconfirmed`` publish: ask the platform whether the post exists. None = can't tell. An
        ``unavailable`` outcome = the platform couldn't be asked just now (rate-limited, unreachable: a retryable
        error on the lookup), so asking again later will tell."""
        if "find" not in platform.capabilities:
            return None
        connected = self.store.get_credentials(user_id, platform.id)
        if not connected:
            return None
        creds, account_data = connected
        try:
            found = await platform.find_existing(creds, Account.from_dict(account_data), prepared)
        except PlatformError as exc:
            if getattr(exc, "retryable", False):
                return Outcome(platform.id, "unavailable", message=str(exc))
            return None
        except Exception:  # a plug-in bug: can't tell, and it must not stop the other posts being resolved
            log.exception("Platform %s crashed looking up an existing post", platform.id)
            return None
        if found is None:
            return Outcome(platform.id, "missing")
        status = found.status if found.status in ("draft", "unlisted") else "published"
        return Outcome(platform.id, status, url=found.url, remote_id=found.remote_id)

    async def recover(self, row: dict[str, Any], now: float | None = None) -> Outcome:
        """A post the process was sending when it stopped. The request may or may not have reached the platform,
        so: ask the platform (``find_existing``) when it can tell; otherwise mark it ``unconfirmed`` for a person.
        Only a post the platform says is *missing* goes back in the queue. A platform that couldn't be asked just
        now (rate-limited, unreachable) is asked again on the next stuck pass, STUCK_AFTER later: the post stays
        ``recovering``, which only ever asks, never sends (#188). That has an end: a post 'recovering' for
        ``recovery_max`` (INKCAST_PUBLISH_RECOVERY_MAX_HOURS, measured from ``row["recovering_since"]``) is
        unconfirmed for a person without another lookup, so it is never sent late on its own (#216): the person
        cancels it or resends it (``resend``), and Git sync never creates it again (#226). A person can cancel a
        'recovering' post meanwhile; every write here is only while it is still 'recovering', with the attempt count
        this pass claimed it at (``fence``): a lookup slow enough to outlast two stuck passes answers a post that may
        have been re-sent and interrupted again since, and that answer is stale (#226)."""
        now = now or time.time()
        post_id, platform = row["id"], self.platforms.get(row["platform"])
        fence = {"only_if": "recovering", "if_attempts": row["attempts"]}
        if platform is None:
            written = self.store.update_post(
                post_id, status="failed", error=f"Platform plug-in {row['platform']!r} is not installed.", **fence,
                events=self._event("post.failed", post_id, row["user_id"], row["platform"], reason="plugin"))
            return self._as_now(written, Outcome(row["platform"], "failed", post_id=post_id))

        def recovered(status: str) -> list[DomainEvent]:
            return self._event("post.recovered", post_id, row["user_id"], platform.id, status=status)

        since = row.get("recovering_since")
        if since is not None and now - float(since) >= self.recovery_max:
            hours = format(self.recovery_max / 3600, "g")
            message = (f"Inkcast restarted while this post was being sent to {platform.name}, and {platform.name} could not "
                       f"be asked whether it arrived for {hours} hours (INKCAST_PUBLISH_RECOVERY_MAX_HOURS). It is not sent "
                       f"again on its own: check {platform.name}, then resend it (inkcast_resend, or POST /api/v1/posts/"
                       f"{post_id}/resend) or cancel it (inkcast_cancel, or POST /api/v1/posts/{post_id}/cancel).")
            written = self.store.update_post(post_id, status="unconfirmed", error=message, events=recovered("unconfirmed"),
                                             **fence)
            if written:  # not when a cancel came first: that post is the person's, not one for the alert (#226)
                self._count(platform.id, "unconfirmed")
                log.warning("Post %s on %s was interrupted mid-send and %s could not be asked for %s hours; now unconfirmed",
                            post_id, platform.id, platform.id, hours,
                            extra={"event": "publish.recovery.capped", "post_id": post_id, "platform": platform.id})
            return self._as_now(written, Outcome(platform.id, "unconfirmed", post_id=post_id, message=message))
        prepared = Prepared.from_dict(json.loads(row["payload"]))
        found = await self.confirm(row["user_id"], platform, prepared)

        # Each answer is written only while the post is still 'recovering' at this pass's attempt count: another worker
        # may have taken it over (a pass longer than STUCK_AFTER), re-queued it and sent it while this lookup was out;
        # or a person cancelled it, and a cancel wins over the answer (#216).
        if found is not None and found.status == "unavailable":
            message = f"Could not check {platform.name} yet ({found.message}); it will be checked again."
            written = self.store.update_post(post_id, status="recovering", error=message, **fence)
            if written:
                log.warning("Post %s on %s was interrupted mid-send and %s could not be asked yet; checking again later",
                            post_id, platform.id, platform.id,
                            extra={"event": "publish.recovery.deferred", "post_id": post_id, "platform": platform.id})
            return self._as_now(written, Outcome(platform.id, "recovering", post_id=post_id, message=message))
        if found is None:
            message = (f"Inkcast restarted while this post was being sent to {platform.name}, which can't be checked "
                       f"automatically. Check {platform.name}, then resend it (inkcast_resend, or POST /api/v1/posts/"
                       f"{post_id}/resend) or cancel it (inkcast_cancel, or POST /api/v1/posts/{post_id}/cancel).")
            written = self.store.update_post(post_id, status="unconfirmed", error=message, events=recovered("unconfirmed"),
                                             **fence)
            outcome = Outcome(platform.id, "unconfirmed", post_id=post_id, message=message)
        elif found.status == "missing":
            written = self.store.update_post(post_id, status="scheduled", run_at=time.time(), events=recovered("scheduled"),
                                             error=f"Interrupted before {platform.name} received it; sending again.",
                                             **fence)
            outcome = Outcome(platform.id, "scheduled", post_id=post_id, message="Interrupted; sending again.")
        else:
            written = self.store.update_post(post_id, status=found.status, result_url=found.url, remote_id=found.remote_id,
                                             error=None, events=recovered(found.status), **fence)
            outcome = Outcome(platform.id, found.status, post_id=post_id, url=found.url, remote_id=found.remote_id)
        outcome = self._as_now(written, outcome)  # the answer as written, or what the post is now (cancelled, re-sent)
        if written:
            self._count(platform.id, "recovered")
        log.warning("Post %s on %s was interrupted mid-send; now %s", post_id, platform.id, outcome.status,
                    extra={"event": "publish.recovered", "post_id": post_id, "platform": platform.id, "status": outcome.status})
        return outcome

    async def run_due(self, now: float | None = None) -> list[Outcome]:
        now = now or time.time()
        outcomes = [await self.recover(row, now) for row in self.store.claim_stuck(older_than=now - STUCK_AFTER, now=now)]
        for row in self.store.claim_due_posts(now):
            if not self.store.renew_claim(row["id"], row["attempts"]):
                continue  # another worker took it over as stuck while this batch was being sent: it is theirs now
            # Lag from when the post could first be sent: its time, or its creation when that came later (a Git file
            # dated in the past is scheduled at that time and due at once, ADR-0015). Measured from its time alone, a
            # file dated a week ago read as a week late on the first tick after it was known, burning the
            # schedule-timeliness SLO and the customer's on-time rate for a post sent as soon as it could be.
            lag = max(0.0, now - max(float(row["run_at"]), float(row["created_at"])))
            self._observe("inkcast_publish_lag_seconds", lag, platform=row["platform"])
            platform = self.platforms.get(row["platform"])
            if platform is None:
                self.store.update_post(row["id"], status="failed", error=f"Platform plug-in {row['platform']!r} is not installed.",
                                       events=self._event("post.failed", row["id"], row["user_id"], row["platform"], reason="plugin"))
                continue
            prepared = Prepared.from_dict(json.loads(row["payload"]))
            outcomes.append(await self._send(row["id"], row["user_id"], platform, prepared, row["attempts"], scheduled=True,
                                             lag=lag, held=row["attempts"]))
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

    def cancel(self, user_id: str, post_id: str, also: Callable[[sqlite3.Connection], None] | None = None) -> str:
        """Cancel a pending post (``cancel_pending``); the title. Git sync comes through here, with its claim on the
        file as ``also``."""
        return self.cancel_pending(user_id, post_id, also=also)[0]

    def cancel_pending(self, user_id: str, post_id: str,
                       also: Callable[[sqlite3.Connection], None] | None = None) -> tuple[str, str]:
        """Cancel a 'scheduled' post, a 'recovering' one (its send was interrupted and the platform has not said whether
        it arrived; the person decides, and the cancel wins over a late answer, #216) or an 'unconfirmed' one (the
        platform may or may not have it; the person decides, #226). The title and the status it was cancelled from:
        'recovering' or 'unconfirmed' means the post may already be on the platform, and its error now says so, so the
        API's reply and the MCP tool's can tell the person honestly. The API, the MCP tool and Git sync all come
        through here. ``also`` is written in the cancel's transaction, only when it cancels (``cancel_if_pending``)."""
        row = self.store.get_post(post_id)
        if not row or row["user_id"] != user_id:
            raise PlatformError(f"No post {post_id!r}.")
        # One conditional write, under the lock, so a scheduler claim can't land between the status check and it.
        name = getattr(self.platforms.get(row["platform"]), "name", row["platform"])
        notes = {
            "recovering": (f"Cancelled while Inkcast was still asking {name} whether an interrupted send arrived: it may "
                           f"already be on {name}. Check {name}, and remove it there if it is."),
            "unconfirmed": (f"Cancelled while {name} had not said whether it arrived: it may already be on {name}. Check "
                            f"{name}, and remove it there if it is."),
        }
        was = self.store.cancel_if_pending(
            post_id, user_id, events=self._event("post.cancelled", post_id, user_id, row["platform"]), notes=notes,
            also=also)
        if not was:
            status = (self.store.get_post(post_id) or row)["status"]
            raise PlatformError(
                f"Post {post_id} is {status}; only scheduled, recovering or unconfirmed posts can be cancelled. Published "
                "stories have to be removed on the platform itself."
            )
        return row["title"], was

    async def resend(self, user_id: str, post_id: str, on_created: Any = None) -> Outcome:
        """A person's decision on one of their 'unconfirmed' posts (#226): send it again, now, as exactly one new post,
        and cancel the old one, whose error then says it may already be on the platform (the person checked first).
        The decision is recorded as a ``post.resent`` event on the old post, with who made it and the new post's id.
        ``on_created(platform_id, new_post_id)`` runs in the transaction that makes the new row, before anything is
        sent (the ledger entries that named the old post follow the new one): if it raises, nothing is made. A second
        resend, or one after a cancel, is refused: ``resend_post`` is one conditional transaction. A retryable failure
        is reported, not queued: the person is there to try again."""
        row = self.store.get_post(post_id)
        if not row or row["user_id"] != user_id:
            raise PlatformError(f"No post {post_id!r}.")
        platform = self.platforms.get(row["platform"])
        if platform is None:
            raise PlatformError(f"Platform plug-in {row['platform']!r} is not installed.")
        new_id = self.store.new_post_id()
        note = (f"Resent by the person as post {new_id}; it may already be on {platform.name} from the first send. Check "
                f"{platform.name}, and remove the extra copy there if it is.")
        if not self.store.resend_post(post_id, user_id, new_id=new_id, note=note,
                                      events=self._event("post.resent", post_id, user_id, platform.id, new_post_id=new_id),
                                      on_created=functools.partial(on_created, platform.id) if on_created else None):
            status = (self.store.get_post(post_id) or row)["status"]
            raise PlatformError(f"Post {post_id} is {status}; only unconfirmed posts can be resent.")
        prepared = Prepared.from_dict(json.loads(row["payload"]))
        return await self._send(new_id, user_id, platform, prepared, attempts=1, held=0)
