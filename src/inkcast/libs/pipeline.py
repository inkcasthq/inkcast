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
from .platforms.base import wait_text
from .config import RetryPolicy
from .db import Store
from .db.posts import backfill
from .content.story import Story
from .events import DomainEvent
from .secrets import CredentialsUnreadable

log = logging.getLogger(__name__)

STUCK_AFTER = 600  # a post 'publishing' for longer than this was interrupted (the HTTP timeout is 30 s)
UPDATE_FENCE_MISSES = 10  # an update's record of its PUT, worded again this many times at most (#473)


def _sentence(text: str) -> str:
    """``text`` ended as a sentence, so the next one reads apart: "Could not reach X: [Errno 61] Connection refused"
    has no full stop of its own (#445)."""
    text = text.rstrip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _named(url: str | None, remote_id: str | None) -> str:
    """A copy of a post as a person can find it: its URL with its id, or whichever it has."""
    if url and remote_id:
        return f"{url} (id {remote_id})"
    return url or f"id {remote_id}"


def _edited(platform: Platform, url: str | None = None, remote_id: str | None = None) -> str:
    """The note on a post whose send's success found it changed meanwhile (#392): what the platform has, and what becomes
    of the change. An update at the next sync where the platform edits posts and the success named the id an update
    needs; by hand otherwise, at the copy named (#427). Recognised by equality, so it is worded from the row's copy
    (``url``, ``remote_id``). ``Publisher.update`` settles it when the update is sent, ``Publisher.settle_edited`` when
    the file is back to what was sent."""
    if "update" in platform.capabilities and remote_id:
        fate = "The next sync sends the change as an update."
    elif "update" in platform.capabilities:
        fate = (f"{platform.name} named no id for it, which an update needs, so Inkcast can't send the change: update "
                f"it by hand: {_named(url, remote_id)}.")
    else:
        fate = f"{platform.name} doesn't let Inkcast edit posts: update it by hand: {_named(url, remote_id)}."
    return f"The post was changed while this send was out, so {platform.name} has it as it was sent. {fate}"


def _lost(platform: Platform) -> str:
    """The note a send's late uncertain answer leaves when it lands over another worker's re-send of the post (#234):
    this request may have arrived too. It outlives the re-send's own success (``PostsRepo.record_success`` keeps it),
    since only a person can say whether the platform has two copies; recognised by equality, like ``_edited``."""
    return (f"An earlier send's answer was lost after its request was sent, and it may have arrived too: a second copy "
            f"may be on {platform.name}. Check {platform.name}, and remove the extra copy there if it is.")


def _undecryptable() -> str:
    """The error a due post's claim records when its credentials could not be decrypted, so its send never began
    (``run_due``): the stuck pass tells it from a send a restart interrupted (#502). Recognised by equality, like
    ``_lost``; every way out of 'recovering' writes another error, so it never outlives the claim it is about. It
    names no platform: a plug-in's display name can change between the claim and the stuck pass."""
    return ("Stored credentials could not be decrypted when this post was due (they were sealed "
            "under another INKCAST_SECRET_KEY than the scheduler runs on), so it was not sent.")


def _never_sent(row: dict[str, Any]) -> bool:
    """A post whose send never began (#502): 'recovering' with the claim's marker, or 'unconfirmed' with the whole
    message the stuck pass wrote from it. An uncertain send stores the plug-in's own error text as 'unconfirmed'
    (``_send``), which could begin with the marker, so the message must also end with this post's own links."""
    error = row["error"] or ""
    if row["status"] == "recovering":
        return error == _undecryptable()
    if row["status"] != "unconfirmed":
        return False
    post_id = row["id"]
    tail = (f"(inkcast_connect, or POST /api/v1/platforms/{row['platform']}/connect-link), then resend it "
            f"(inkcast_resend, or POST /api/v1/posts/{post_id}/resend) or cancel it "
            f"(inkcast_cancel, or POST /api/v1/posts/{post_id}/cancel).")
    return error.startswith(_undecryptable() + " Reconnect ") and error.endswith(tail)


class Disconnected(PlatformError):
    """A resend refused before anything is cancelled: the platform isn't connected, or refused the stored credentials
    (#234). The message names the reconnect step."""


class Unavailable(PlatformError):
    """A resend refused before anything is cancelled: the platform couldn't be asked just now whether it still accepts
    the stored credentials (a retryable error on ``verify``: rate-limited, unreachable), so the send would very likely
    fail the same way, and a resend's send is never queued (#445). The message names when to try again: the platform's
    Retry-After when it gave one."""


class AlreadyResent(PlatformError):
    """A resend refused because the post was resent already: ``new_post_id`` is the post that resend made, for a
    client whose first reply was lost to follow (#234)."""

    def __init__(self, message: str, new_post_id: str):
        super().__init__(message)
        self.new_post_id = new_post_id


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
            platform = self.platforms[pid]
            try:
                ready.append(platform.prepare(story.for_platform(pid), story.body))
            except PlatformError as exc:
                problems.append(Outcome(pid, "failed", message=str(exc)))
            except Exception:
                # A plug-in bug on this story's values, with no I/O done: this platform fails, and the others, and a
                # sync's other files, are still answered, instead of a 500 for the whole request (#130). The
                # exception's text stays in the log: it may quote the story.
                log.exception("Platform %s crashed preparing a story", pid)
                problems.append(Outcome(pid, "failed", message=(
                    f"Internal error in the {platform.name} plug-in while preparing this story; check its `{pid}:` "
                    "block in the front matter.")))
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
            refused = await self.refusal(user_id, story, platform, prepared, retry=retry)
            if refused is not None:
                outcomes.append(refused)
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

    async def refusal(self, user_id: str, story: Story, platform: Platform, prepared: Prepared,
                      retry: bool = False) -> Outcome | None:
        """Why ``prepared`` can't go to ``platform``: not connected ('skipped'), or a destination that doesn't resolve
        ('failed'). None when it can. Checked before a post is made, and before Git sync changes a post waiting for its
        retry (#204), so a typo fails now, not at 3am. ``retry`` as for ``publish``."""
        connected = self.store.get_credentials(user_id, platform.id)
        if not connected:
            help_text = platform.not_connected_help(story.for_platform(platform.id))
            return Outcome(platform.id, "skipped", message=(
                f"{platform.name} isn't connected. Use inkcast_connect to add it."
                + (f" {help_text}" if help_text else "")
            ))
        creds, account = connected
        try:
            if prepared.destination:
                await platform.resolve_destination(creds, Account.from_dict(account), prepared.destination)
        except PlatformError as exc:
            # A lookup that will pass (a 429) is no typo: when asked to retry, the send resolves it again, and
            # its retryable failure goes to the scheduler under the policy and the Retry-After (#188).
            if not (retry and getattr(exc, "retryable", False)):
                return Outcome(platform.id, "failed", message=str(exc))
        return None

    async def _send(self, post_id: str, user_id: str, platform: Platform, prepared: Prepared, attempts: int,
                    scheduled: bool = False, lag: float | None = None, retry: bool = False, held: int = 0,
                    connected: tuple[dict[str, Any], dict[str, Any]] | None = None) -> Outcome:
        """``scheduled``: sent by the scheduler at its time (recorded on the event). ``retry``: a retryable failure
        is put back in the queue under the policy; always for the scheduler, for a "now" post only when asked.
        ``held``: the row's ``attempts`` while this send holds the post (the scheduler's claim counted its own; a
        "now" post's row still reads 0): the fence of the send's failure writes, like ``renew_claim``'s (#226).
        ``connected``: the credentials a resend read and verified before it cancelled the old post, sent with as they
        are: read again here, a reseal under another key meanwhile would fail the send after that cancel (#472)."""
        retry = retry or scheduled
        connected = connected or self.store.get_credentials(user_id, platform.id)
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
        # What this send carries, read before the plug-in's call: a plug-in that changes the ``Prepared`` it is handed
        # would otherwise leave a row that never hashes as the file, and Git sync would send it again at every sync (#503).
        sent = prepared.to_dict()
        started = time.perf_counter()
        try:
            destination = (
                await platform.resolve_destination(creds, account, prepared.destination) if prepared.destination else None
            )
            result = await platform.publish(creds, account, prepared, destination)
            if not result.url and not result.remote_id:
                # A 2xx that names nothing (a 204, a 202 with no body, JSON without the post): the platform accepted
                # the request, so the post is probably out, but nothing says where, and nothing could find it again.
                # Never 'published' on that (#214): it is as uncertain as a lost answer, and handled below as one.
                raise PlatformError(
                    f"{platform.name} accepted the request but its answer named neither the post's URL nor its id, so "
                    f"it may or may not have been published. Check {platform.name} before trying again.", uncertain=True)
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
                # this request may have arrived too, and the note says so (``_lost``), so that the re-send's own
                # success, landing after, keeps the flag that a person checks for a second copy (#234).
                written = self.store.update_post(post_id, status="unconfirmed", error=str(exc), unless="cancelled",
                                                 unless_found_at=held, beyond=f"{exc}; {_lost(platform)}",
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
                # not_before: the wait an author's edit during it keeps (``PostsRepo.edit_waiting``, #204).
                after = time.time() + delay
                written = self.store.update_post(
                    post_id, status="scheduled", run_at=after, not_before=after, attempts=attempts, error=f"retrying: {exc}",
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
        # The platform's success is the truth, and wins (#214): it is written over whatever the post became while this
        # send was out past STUCK_AFTER. Taken over as stuck; cancelled (the post is out, and the person needs its URL to
        # remove it: #216); re-sent by another worker and queued again, 'failed' or cancelled (that request never took
        # effect: this copy is the only one). A re-send still out, or answered without saying (recovering, unconfirmed,
        # cancelled from one of those), may have arrived too: this copy is recorded and that one noted, in #226's words,
        # for the person to check; its own late answers then fall to the fences above (a failure or a 429 over a
        # published post is refused, so nothing is sent again, and the URL stays). The one thing a success never
        # replaces is a confirmed copy of another post already on the row (``PostsRepo.record_success``): a re-send
        # that arrived, or a lookup's match (DEV's matches by title, so an older article). Nothing says which of the two
        # is the extra one, so the row keeps the copy it has, names both, and a person keeps one. The read that words
        # a note and the write are one transaction, so a claim can't land between them.
        def second_copy(attempt: int) -> str:
            return (f"Attempt {attempt} was sent by another worker while this one was out, and may have arrived too: a "
                    f"second copy may be on {platform.name}. Check {platform.name}, and remove the extra copy there if it is.")

        def both_copies(row: dict[str, Any]) -> str:
            return (f"{platform.name} may have this post twice: {_named(row['result_url'], row['remote_id'])}, recorded "
                    f"here, and {_named(result.url, result.remote_id)}, attempt {attempts}'s answer. Check "
                    f"{platform.name}: keep one and remove the other.")

        # The platform has what this send carried, whatever the row came to hold meanwhile: Git sync's edit in place of
        # a post recovery had queued again while this send was still out (#392). The row takes the sent content back,
        # with a note, and the file's entry sees the edit at the next sync: sent as an update where the platform edits
        # posts, reported as stale (update it by hand) where it doesn't. Never a second post, and never dropped.
        edited, lost = _edited(platform, result.url, result.remote_id), _lost(platform)

        written, before = self.store.record_success(
            post_id, held=held, status=status, result_url=result.url, remote_id=result.remote_id, sent=sent,
            notes=result.notes, second_copy=second_copy, both_copies=both_copies, edited=edited, lost=lost,
            events=self._event("post.published", post_id, user_id, platform.id, status=status, scheduled=scheduled,
                               attempts=attempts, lag_seconds=round(lag, 3) if lag is not None else None))
        if written or before is None:  # before is None: erased meanwhile, the row left alone and the answer kept
            notes = list(result.notes)
            if written:
                self._count(platform.id, status)
                if lost in self.store.notes_of(before):  # an earlier send's uncertain answer landed over this re-send
                    notes.append(lost)
                    log.warning("Post %s on %s was published by a re-send; an earlier send's answer was lost after its "
                                "request was sent, and may have posted a second copy", post_id, platform.id,
                                extra={"event": "publish.second_copy", "post_id": post_id, "platform": platform.id})
                if self.store.may_have_arrived_since(before, held):
                    notes.append(second_copy(before["attempts"]))
                    log.warning("Post %s on %s was published by a send taken over as stuck; attempt %s, sent meanwhile, "
                                "may have posted a second copy", post_id, platform.id, before["attempts"],
                                extra={"event": "publish.second_copy", "post_id": post_id, "platform": platform.id})
                if not self.store.holds(before, sent):
                    notes.append(edited)
                    log.warning("Post %s on %s was changed while its send was out; %s has it as it was sent, and the "
                                "change waits for the next sync", post_id, platform.id, platform.id,
                                extra={"event": "publish.edited_meanwhile", "post_id": post_id, "platform": platform.id})
            return Outcome(platform.id, status, post_id=post_id, url=result.url, notes=notes, prepared=prepared,
                           remote_id=result.remote_id)
        log.warning("Post %s on %s: attempt %s answered %s (%s), and the post records another copy (%s); %s may have "
                    "both, and a person keeps one", post_id, platform.id, attempts, status,
                    _named(result.url, result.remote_id), _named(before["result_url"], before["remote_id"]), platform.id,
                    extra={"event": "publish.second_copy", "post_id": post_id, "platform": platform.id})
        return Outcome(platform.id, before["status"], post_id=post_id, url=before["result_url"],
                       remote_id=before["remote_id"], message=both_copies(before))

    def _crashed(self, post_id: str, user_id: str, platform: Platform, exc: BaseException, held: int = 0) -> Outcome:
        """A plug-in bug while sending. It may have crashed after the platform accepted the post (reading the reply),
        so it is ambiguous like any uncertain failure: unconfirmed, never 'failed', which Git sync would create again."""
        written = self.store.update_post(post_id, status="unconfirmed", error=f"internal error: {exc}", unless="cancelled",
                                         unless_found_at=held, beyond=f"internal error: {exc}; {_lost(platform)}",
                                         events=self._event("post.unconfirmed", post_id, user_id, platform.id))  # as _deliver's
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
        sent = json.dumps(prepared.to_dict())  # before the plug-in's call, as ``_deliver``'s (#503)
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
        if post_id:  # a PUT's answer that names no URL (a 204) keeps the post's: the post is where it was (#214)
            # The update that sends an edit made while a send was out settles that edit's note only (#392): the success
            # that wrote it may have noted a second copy beside it (#214), which is the person's to check.
            # Worded from a read of the notes and written only while they are as read (#473): a note written meanwhile
            # (another worker's late success naming a second copy, ``PostsRepo.record_success``) makes the write miss,
            # and the notes are worded again from the row as it is now, keeping every note the first read did not hold:
            # it is news this update was never a decision about. Not given up on one miss: the platform has this update,
            # and a row left with the content it replaced is what a later success or follow compares with (#392). Given
            # up after UPDATE_FENCE_MISSES, with an error: the loop holds the event loop (nothing in it awaits), and the
            # file's entry, not updated either, sends the same PUT again at the next sync.
            first: list[str] | None = None
            misses = 0
            while (row := self.store.get_post(post_id)) is not None:  # None: erased meanwhile, nothing to record
                notes = list(result.notes)
                held, edited = self.store.notes_of(row), _edited(platform, row["result_url"], row["remote_id"])
                if edited in held:
                    notes = [n for n in held if n != edited] + notes
                elif _lost(platform) in held:  # an edit is no decision about a second copy: only a person clears it (#234)
                    notes = [_lost(platform)] + notes
                first = held if first is None else first
                notes += [n for n in held if n and n != edited and n not in first and n not in notes]
                if self.store.update_post(post_id, status=status, title=prepared.title,
                                          payload=sent, error="; ".join(notes) or None,
                                          if_error=row["error"] or "", **({"result_url": result.url} if result.url else {})):
                    break
                misses += 1
                if misses == UPDATE_FENCE_MISSES:
                    raise RuntimeError(f"Post {post_id} kept changing while its update was recorded; sync it again.")
        self._count(platform.id, "updated")
        return Outcome(platform.id, status, post_id=post_id, url=result.url, notes=result.notes, prepared=prepared,
                       remote_id=result.remote_id or remote_id)

    def settle_edited(self, post_id: str, platform: Platform, holds: Callable[[dict[str, Any]], bool]) -> bool:
        """Clear the note that a post was changed while its send was out (``_edited``) once nothing is owed: Git sync
        found the file back to what was sent (#427), and the post, which records what the platform received, holds the
        file (``holds(row)``, on the same read as the notes). The file's entry has the hash of what was sent only once
        it has followed the post: a success landing on a post already confirmed with its copy (such as a lookup's match on
        less than the whole payload, then that send's late answer) writes the note and the content sent with no follow, and
        the change the note names is still owed (#473): the next sync sends it (``GitSync._one``, #503). Every other note
        stays (a second copy, an earlier send's lost answer: the person's to check). Worded from the row's copy and
        matched by equality, as ``update`` matches it, and written only while the row is as read (status, attempts, notes
        and payload): a note written meanwhile (a second copy, ``PostsRepo.record_success``) is never overwritten, nor a
        note settled for content the post no longer holds. True when cleared."""
        row = self.store.get_post(post_id)
        if row is None or not holds(row):
            return False
        held, edited = self.store.notes_of(row), _edited(platform, row["result_url"], row["remote_id"])
        if edited not in held:
            return False
        kept = [n for n in held if n != edited]
        return self.store.update_post(post_id, error="; ".join(kept) or None, only_if=row["status"],
                                      if_attempts=row["attempts"], if_error=row["error"], if_payload=row["payload"])

    async def confirm(self, user_id: str, platform: Platform, prepared: Prepared) -> Outcome | None:
        """For an ``unconfirmed`` publish: ask the platform whether the post exists. None = the platform can't be
        checked (no lookup, not connected, the lookup refused, the plug-in crashed). An ``unconfirmed`` outcome = the
        platform was asked and could not say which post is this one; its ``message`` is the reason, worded for the
        person with the ids it names (#428). An ``unavailable`` outcome = the platform couldn't be asked just now
        (rate-limited, unreachable: a retryable error on the lookup), so asking again later will tell."""
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
        if not found.url and not found.remote_id:
            # The platform has something, says the plug-in, but nothing names this post: the plug-in could not name
            # what it found, or could not say which of several is the post (DEV, #396; the reason in its notes). No post
            # is recorded as published on that, from recovery or from Git sync's check (#214), any more than from a
            # send. Can't tell: a person decides, told the reason (#428): the plug-in's notes, which name what the person
            # should look at (DEV: the article ids) in fixed text, never content of the platform's answer.
            reason = "; ".join(found.notes) or f"{platform.name} found a post it could not name as this one"
            log.warning("Platform %s found a post it could not name as this one: %s", platform.id, reason)
            return Outcome(platform.id, "unconfirmed", message=reason)
        status = found.status if found.status in ("draft", "unlisted") else "published"
        return Outcome(platform.id, status, url=found.url, remote_id=found.remote_id)

    async def recover(self, row: dict[str, Any], now: float | None = None) -> Outcome:
        """A post the process was sending when it stopped. The request may or may not have reached the platform,
        so: ask the platform (``find_existing``) when it can tell; otherwise mark it ``unconfirmed`` for a person. One
        whose send never began (its credentials could not be decrypted: ``run_due`` records it) is no such post: where
        the platform can't be asked, its owner is told so, and to reconnect (#502). Only a post the platform says is
        *missing* goes back in the queue. A platform that couldn't be asked just now (rate-limited, unreachable) is
        asked again on the next stuck pass, STUCK_AFTER later: the post stays ``recovering``, which only ever asks,
        never sends (#188). That has an end: a post 'recovering' for
        ``recovery_max`` (INKCAST_PUBLISH_RECOVERY_MAX_HOURS, measured from ``row["recovering_since"]``) is asked
        about one last time and, unless the platform has it, is unconfirmed for a person, "missing" included, so it
        is never sent late on its own (#216): the person cancels it or resends it (``resend``), and Git sync never
        creates it again (#226). The last lookup is what a post re-sent and interrupted again past the cap needs: its
        clock is the first interruption's, and its second send may have arrived (#234). A person can cancel a
        'recovering' post meanwhile; every write here is only while it is still 'recovering', with the attempt count
        this pass claimed it at (``fence``): a lookup slow enough to outlast two stuck passes answers a post that may
        have been re-sent and interrupted again since, and that answer is stale (#226). The one exception is a found
        answer, written over 'unconfirmed' too at the same attempt count: the sender's uncertain answer, or another
        pass's cap, landed while this lookup was out, and a post the platform has must not be left for the person to
        resend, a second copy (#234)."""
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

        if "find" not in platform.capabilities and row["error"] == _undecryptable():
            # The claim's record that this send never began (``run_due``), on a platform that can't be asked: no
            # restart, and nothing was sent. Its owner is told what happened, and decides (#502). Counted unconfirmed,
            # where InkcastUnconfirmedPublishes sees it: InkcastPostsCannotBeSent ends 30 minutes after the send.
            message = (f"{_undecryptable()} Reconnect {platform.name} (inkcast_connect, or POST /api/v1/platforms/"
                       f"{platform.id}/connect-link), then resend it (inkcast_resend, or POST /api/v1/posts/{post_id}/"
                       f"resend) or cancel it (inkcast_cancel, or POST /api/v1/posts/{post_id}/cancel).")
            written = self.store.update_post(post_id, status="unconfirmed", error=message, events=recovered("unconfirmed"),
                                             **fence)
            if written:  # not over a cancel, as the cap's count (#226)
                self._count(platform.id, "unconfirmed")
                log.warning("Post %s on %s was never sent: its credentials could not be decrypted when it was due; now "
                            "unconfirmed for its owner", post_id, platform.id,
                            extra={"event": "publish.recovery.unsent", "post_id": post_id, "platform": platform.id})
            return self._as_now(written, Outcome(platform.id, "unconfirmed", post_id=post_id, message=message))

        since = row.get("recovering_since")
        capped = since is not None and now - float(since) >= self.recovery_max
        try:
            found = await self.confirm(row["user_id"], platform, Prepared.from_dict(json.loads(row["payload"])))
        except Exception:
            if not capped:
                raise
            # Past the cap, a lookup that can't even be made (credentials sealed under another INKCAST_SECRET_KEY:
            # ``confirm`` reads them outside its guard; a payload this version can't read, after a rollback) must not
            # hold the post 'recovering' for ever, nor every post this pass claimed after it: it ends as it did before
            # the cap asked, "could not be asked" (#216, #234).
            log.exception("Post %s on %s: the cap's last lookup could not be made", post_id, platform.id)
            found = None

        # Each answer is written only while the post is still 'recovering' at this pass's attempt count: another worker
        # may have taken it over (a pass longer than STUCK_AFTER), re-queued it and sent it while this lookup was out;
        # or a person cancelled it, and a cancel wins over the answer (#216).
        if found is not None and found.status in self.store.CONFIRMED:
            # The platform has it: the truth, whatever the cap says, and over a post unconfirmed meanwhile (#234). An
            # earlier send's answer that landed meanwhile said a second copy may be there too (``_lost``): a lookup that
            # finds one copy doesn't say there is no other, so that note stays (``keep``).
            written = self.store.update_post(post_id, status=found.status, result_url=found.url, remote_id=found.remote_id,
                                             error=None, keep=_lost(platform), events=recovered(found.status),
                                             only_if=("recovering", "unconfirmed"), if_attempts=row["attempts"])
            outcome = Outcome(platform.id, found.status, post_id=post_id, url=found.url, remote_id=found.remote_id)
        elif capped:
            hours = format(self.recovery_max / 3600, "g")
            said = (f"{platform.name} says it isn't there, but it is {hours} hours since the send was interrupted"
                    if found is not None and found.status == "missing"
                    # The platform's reason it can't tell, with the ids it names (#428)
                    else f"{(found.message or '').rstrip('.')}, and it is {hours} hours since the send was interrupted"
                    if found is not None and found.status == "unconfirmed"
                    else f"{platform.name} could not be asked whether it arrived for {hours} hours")
            message = (f"Inkcast restarted while this post was being sent to {platform.name}, and {said} "
                       f"(INKCAST_PUBLISH_RECOVERY_MAX_HOURS). It is not sent again on its own: check {platform.name}, then "
                       f"resend it (inkcast_resend, or POST /api/v1/posts/{post_id}/resend) or cancel it (inkcast_cancel, "
                       f"or POST /api/v1/posts/{post_id}/cancel).")
            written = self.store.update_post(post_id, status="unconfirmed", error=message, events=recovered("unconfirmed"),
                                             **fence)
            if written:  # not when a cancel came first: that post is the person's, not one for the alert (#226)
                self._count(platform.id, "unconfirmed")
                log.warning("Post %s on %s was interrupted mid-send and %s has not confirmed it in %s hours; now unconfirmed",
                            post_id, platform.id, platform.id, hours,
                            extra={"event": "publish.recovery.capped", "post_id": post_id, "platform": platform.id})
            return self._as_now(written, Outcome(platform.id, "unconfirmed", post_id=post_id, message=message))
        elif found is not None and found.status == "unavailable":
            message = f"Could not check {platform.name} yet ({found.message}); it will be checked again."
            written = self.store.update_post(post_id, status="recovering", error=message, **fence)
            if written:
                log.warning("Post %s on %s was interrupted mid-send and %s could not be asked yet; checking again later",
                            post_id, platform.id, platform.id,
                            extra={"event": "publish.recovery.deferred", "post_id": post_id, "platform": platform.id})
            return self._as_now(written, Outcome(platform.id, "recovering", post_id=post_id, message=message))
        elif found is None or found.status == "unconfirmed":
            # Can't be checked at all, or checked and the platform can't say which post is this one: its reason, with
            # the ids it names, is the person's to act on (#428).
            said = ("which can't be checked automatically" if found is None
                    else f"and {(found.message or '').rstrip('.')}")
            message = (f"Inkcast restarted while this post was being sent to {platform.name}, {said}. Check "
                       f"{platform.name}, then resend it (inkcast_resend, or POST /api/v1/posts/{post_id}/resend) or cancel "
                       f"it (inkcast_cancel, or POST /api/v1/posts/{post_id}/cancel).")
            written = self.store.update_post(post_id, status="unconfirmed", error=message, events=recovered("unconfirmed"),
                                             **fence)
            outcome = Outcome(platform.id, "unconfirmed", post_id=post_id, message=message)
        else:  # missing, within the cap: the request never arrived, so it goes back in the queue
            again = time.time()
            written = self.store.update_post(post_id, status="scheduled", run_at=again, not_before=again,
                                             events=recovered("scheduled"),
                                             error=f"Interrupted before {platform.name} received it; sending again.",
                                             **fence)
            outcome = Outcome(platform.id, "scheduled", post_id=post_id, message="Interrupted; sending again.")
        outcome = self._as_now(written, outcome)  # the answer as written, or what the post is now (cancelled, re-sent)
        if written:
            self._count(platform.id, "recovered")
        log.warning("Post %s on %s was interrupted mid-send; now %s", post_id, platform.id, outcome.status,
                    extra={"event": "publish.recovered", "post_id": post_id, "platform": platform.id, "status": outcome.status})
        return outcome

    async def run_due(self, now: float | None = None) -> list[Outcome]:
        now = now or time.time()
        outcomes = []
        for row in self.store.claim_stuck(older_than=now - STUCK_AFTER, now=now):
            try:
                outcomes.append(await self.recover(row, now))
            except sqlite3.Error:
                raise  # the store, not this row: the next row's write would fail the same way, so the tick stops
            except Exception:
                # One row whose recovery can't even be made (its credentials sealed under another INKCAST_SECRET_KEY,
                # which ``confirm`` reads outside its guard; a payload this version can't read) must not end the pass
                # for every row claimed after it, other people's posts included, for the whole of the cap (#446). The
                # row stays 'recovering', with its clock, and is taken again at the next stuck pass; past the cap
                # ``recover`` ends it. Counted and logged by post id, so an alert can name a row that keeps failing.
                log.exception("Post %s on %s: its recovery could not be made; left for the next stuck pass", row["id"],
                              row["platform"], extra={"event": "publish.recovery.failed", "post_id": row["id"],
                                                      "platform": row["platform"]})
                self._count(row["platform"], "recovery_failed")
        for row in self.store.claim_due_posts(now):
            if not self.store.renew_claim(row["id"], row["attempts"]):
                continue  # another worker took it over as stuck while this batch was being sent: it is theirs now
            # Lag: this clock against the time as written, one measure on one clock. A backfill (made after its time: a
            # Git file dated in the past, due at once, ADR-0015) has no time to be on time for, so its send is not
            # measured: not in the histogram (the schedule-timeliness SLO) and not in the customer's on-time rate
            # (``lag_seconds`` None on the event). Measured from its creation, an import of 200 files burnt the SLO at
            # the batch rate, and the API pod's stamp against this clock read as lag (ADR-0019).
            lag = None if backfill(row) else max(0.0, now - float(row["run_at"]))
            if lag is not None:
                self._observe("inkcast_publish_lag_seconds", lag, platform=row["platform"])
            platform = self.platforms.get(row["platform"])
            if platform is None:
                self.store.update_post(row["id"], status="failed", error=f"Platform plug-in {row['platform']!r} is not installed.",
                                       events=self._event("post.failed", row["id"], row["user_id"], row["platform"], reason="plugin"))
                continue
            try:
                prepared = Prepared.from_dict(json.loads(row["payload"]))
                outcomes.append(await self._send(row["id"], row["user_id"], platform, prepared, row["attempts"],
                                                 scheduled=True, lag=lag, held=row["attempts"]))
            except sqlite3.Error:
                raise  # the store, not this post: as in recovery, the tick stops
            except Exception as exc:
                # Raised outside the send's own guard (credentials sealed under another INKCAST_SECRET_KEY, a payload this
                # version can't read): the post stays claimed, so recovery takes it after STUCK_AFTER, as when the tick
                # stopped here, and the batch's other posts, other people's too, are sent now (#446). Counted, as a
                # recovery that can't be made is: the scheduler goes on ticking, and on a platform that can't be asked
                # (Medium) recovery never reads the credentials, so this is the count every such post makes (#472).
                log.exception("Post %s on %s could not be sent; left for recovery", row["id"], row["platform"],
                              extra={"event": "publish.send.failed", "post_id": row["id"], "platform": row["platform"]})
                self._count(row["platform"], "send_failed")
                if isinstance(exc, CredentialsUnreadable):
                    # Raised by ``_send``'s read of the credentials: everything from the plug-in's call on is guarded
                    # there, so this send never began. The claim records it for the stuck pass (#502), only while it is
                    # still this claim: another worker may have taken the post over and resolved it meanwhile.
                    self.store.update_post(row["id"], error=_undecryptable(), only_if="publishing",
                                           if_attempts=row["attempts"])
        return outcomes

    async def run_forever(self, interval: float, stop: asyncio.Event, housekeeping: Any = None,
                          on_tick: Any = None) -> None:
        """Tick until ``stop`` is set. A tick in progress always finishes (no half-sent posts on SIGTERM).
        ``housekeeping`` is called after every tick, whether the tick succeeded or failed, on its own: a failure of
        either is logged (``scheduler.tick.failed``, ``scheduler.housekeeping.failed``) and costs the other nothing.
        How often it does anything is its own business (the scheduler's runs every HOUSEKEEPING_EVERY, timed in the
        database so a restart does not put it off; it ran on every 90th successful tick before, so a scheduler that
        restarted more often, or whose ticks kept failing, never purged; issue #141). It runs in a worker thread: its
        checkpoint can wait WAL_CHECKPOINT_WAIT_MS for a reader, and the loop serves meanwhile (#470)."""
        log.info("Scheduler running every %.0fs", interval)
        while not stop.is_set():
            try:
                for o in await self.run_due():
                    log.info("Scheduled post %s on %s: %s %s", o.post_id, o.platform, o.status, o.url or o.message or "",
                             extra={"post_id": o.post_id, "platform": o.platform, "status": o.status})
                self.last_tick = time.time()
                if on_tick:
                    on_tick()
                if self.metrics:
                    self.metrics.inc("inkcast_scheduler_ticks_total")
            except Exception:
                log.exception("Scheduler tick failed", extra={"event": "scheduler.tick.failed"})
            if housekeeping:
                try:
                    await asyncio.to_thread(housekeeping)
                except Exception:
                    log.exception("Housekeeping failed", extra={"event": "scheduler.housekeeping.failed"})
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
        never_sent = _never_sent(row)
        notes = {
            "recovering": (f"Cancelled while Inkcast was still asking {name} whether an interrupted send arrived: it may "
                           f"already be on {name}. Check {name}, and remove it there if it is."),
            "unconfirmed": (f"Cancelled while {name} had not said whether it arrived: it may already be on {name}. Check "
                            f"{name}, and remove it there if it is."),
        }
        if never_sent:
            notes["recovering"] = notes["unconfirmed"] = f"Cancelled by the person. {_undecryptable()}"
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
        resend, or one after a cancel, is refused: ``resend_post`` is one conditional transaction; a retried resend
        (its first reply lost) is told the post the first one made (``AlreadyResent``, #234). Refused too, before
        anything is cancelled, while the platform isn't connected or refuses the stored credentials (``verify``, the
        check a connect makes): the old post would be cancelled for a new one that fails at once, and the decision
        consumed (``Disconnected``, naming the reconnect, #234); so are credentials sealed under another
        INKCAST_SECRET_KEY, which the send could not read either (#472). A check that can't be made just now (a 429, unreachable)
        refuses it too, for now (``Unavailable``, naming the platform's Retry-After, #445): the send would very likely
        fail the same way, and a resend's send is never queued. A retryable failure of the send is reported, not
        queued: the person is there to try again."""
        row = self.store.get_post(post_id)
        if not row or row["user_id"] != user_id:
            raise PlatformError(f"No post {post_id!r}.")
        platform = self.platforms.get(row["platform"])
        if platform is None:
            raise PlatformError(f"Platform plug-in {row['platform']!r} is not installed.")
        if row["status"] != "unconfirmed":  # before the connection: a retried resend is told the post the first made
            raise self._not_resendable(post_id, row)
        reconnect = (f"Reconnect {platform.name} (inkcast_connect, or POST /api/v1/platforms/{platform.id}/connect-link), "
                     f"then resend post {post_id}.")

        def stored() -> tuple[dict[str, Any], dict[str, Any]] | None:
            try:
                return self.store.get_credentials(user_id, platform.id)
            except CredentialsUnreadable as exc:
                # Sealed under another key: the send would raise the same, after the old post is cancelled (#472).
                log.warning("Post %s on %s: its credentials can't be decrypted; the resend is refused", post_id,
                            platform.id, extra={"event": "publish.resend.unreadable", "post_id": post_id,
                                                "platform": platform.id})
                raise Disconnected(f"{platform.name}'s stored credentials can't be decrypted (they were sealed under "
                                   f"another INKCAST_SECRET_KEY): post {post_id} was not resent and is still "
                                   f"unconfirmed. {reconnect}") from exc

        connected = stored()
        if connected:
            try:
                await platform.verify(connected[0])
            except PlatformError as exc:
                if not exc.retryable:
                    raise Disconnected(f"{exc} Post {post_id} was not resent and is still unconfirmed. {reconnect}") from exc
                # A check that can't be made just now (a 429, unreachable): the send would very likely fail the same
                # way, and a resend's send is never queued, so the old post would be cancelled for a new one that fails
                # at once (#445). Refused instead, for now: the wait is the platform's Retry-After, when it gave one,
                # in the words its own 429 uses, so a reply that quotes both states one wait.
                raise Unavailable(f"{_sentence(str(exc))} Post {post_id} was not resent and is still unconfirmed: "
                                  f"resend it {wait_text(exc.retry_after)}.") from exc
            # Read again: a disconnect while verify was out would cancel the old post for a new one that fails at once.
            connected = stored()
        if not connected:
            raise Disconnected(f"{platform.name} isn't connected: post {post_id} was not resent and is still unconfirmed. "
                               f"{reconnect}")
        # Before the cancel too: a payload this version can't read (a rollback) would fail the send after it (#472).
        prepared = Prepared.from_dict(json.loads(row["payload"]))
        new_id = self.store.new_post_id()
        note = (f"Resent by the person as post {new_id}. {_undecryptable()}"
                if _never_sent(row) else
                f"Resent by the person as post {new_id}; it may already be on {platform.name} from the first send. Check "
                f"{platform.name}, and remove the extra copy there if it is.")
        events = [*self._event("post.cancelled", post_id, user_id, platform.id, reason="resent"),
                  *self._event("post.resent", post_id, user_id, platform.id, new_post_id=new_id)]
        if not self.store.resend_post(post_id, user_id, new_id=new_id, note=note, events=events,
                                      on_created=functools.partial(on_created, platform.id) if on_created else None):
            raise self._not_resendable(post_id, self.store.get_post(post_id) or row)
        return await self._send(new_id, user_id, platform, prepared, attempts=1, held=0, connected=connected)

    def _not_resendable(self, post_id: str, current: dict[str, Any]) -> PlatformError:
        """Why ``current`` can't be resent: it isn't 'unconfirmed'. One resent already names the post that resend made
        (``AlreadyResent``), whatever the platform's connection is now, so a client whose first reply was lost follows
        it instead of publishing by hand (#234)."""
        message = f"Post {post_id} is {current['status']}; only unconfirmed posts can be resent."
        if current.get("resent_as"):
            made = self.store.get_post(current["resent_as"])
            return AlreadyResent(f"{message} It was resent as post {current['resent_as']}, which is "
                                 f"{made['status'] if made else 'gone'}: follow that one.", current["resent_as"])
        return PlatformError(message)
