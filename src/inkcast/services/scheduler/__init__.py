# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""SchedulerWorker: sends scheduled posts when they are due, and keeps the stores tidy.

It drains the posts queue (``claim_due_posts`` is atomic, so replicas never double-send),
retries only failures that certainly didn't publish, records a heartbeat every tick (visible to
every process through the store), and every HOUSEKEEPING_EVERY runs ``housekeeping`` (expired grants
and tickets, the outbox and KPI retentions), timed by a second heartbeat so a restart or a failing
tick never puts it off. It serves no HTTP except probes and /metrics on the metrics port. Overdue
posts show up as the ``inkcast_posts_overdue`` gauge, which the InkcastPostsOverdue alert watches.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ...libs.events import CONSUMERS
from ...libs.runtime import Context
from ...libs.service import ServiceSpec

log = logging.getLogger(__name__)

HEARTBEAT = "scheduler"
HOUSEKEEPING = "housekeeping"     # the heartbeat that times housekeeping: when it last ran, in any scheduler process
# Seconds between housekeeping runs: what 90 ticks of the default 20 s loop were. A technical cadence, not a policy:
# the retentions (INKCAST_*_RETENTION_DAYS) say what is kept; this says how soon what they release is freed.
HOUSEKEEPING_EVERY = 30 * 60.0
# A cursor under a name CONSUMERS lacks is retired only once no poller has touched it for this long: a live poller
# refreshes its row every pass (INKCAST_INSIGHTS_INTERVAL, 10 s), and during a rollout or a rollback a pod of another
# release can poll a name this release does not know. Two housekeeping runs: a retired name goes within 90 minutes.
RETIRE_IDLE = 2 * HOUSEKEEPING_EVERY


def sync_consumers(ctx: Context) -> None:
    """The outbox cursors made to match the code's consumers (``CONSUMERS``; issue #141), so a retired consumer's cursor
    can't hold the purge and one that has not run yet is waited for: at the scheduler's start, so a cursor left
    long idle holds nothing from the first tick on, and in every housekeeping. A retired name goes once no poller has
    touched it for RETIRE_IDLE, never while a pod of another release still polls it. Each change is logged with the
    consumer's name: the code's own, never a person's."""
    retired, registered = ctx.store.sync_consumers(CONSUMERS, RETIRE_IDLE)
    for name in retired:
        log.warning("Outbox consumer %s is no longer in the code: its cursor is removed, and the events only it had not "
                    "passed are purged by retention", name, extra={"event": "outbox.consumer.retired", "consumer": name})
    for name in registered:
        log.info("Outbox consumer %s has not run yet: its cursor starts at the beginning, and the purge waits for it",
                 name, extra={"event": "outbox.consumer.registered", "consumer": name})


def housekeeping(ctx: Context) -> None:
    """Every HOUSEKEEPING_EVERY (``housekeeping_if_due``): the outbox cursors matched to the code's consumers
    (``sync_consumers``); expired grants and tickets, then domain events and hourly KPI buckets past the configured
    retention (INKCAST_OUTBOX_RETENTION_DAYS, INKCAST_KPI_HOURLY_RETENTION_DAYS; F1.10.S3, issue #91); and, once per
    database, the rows left by writes in the instant of an erasure before the guard refused them (issue #196), logged
    as counts only: no new erasure, and no audit row."""
    sync_consumers(ctx)
    ctx.store.purge_expired(ctx.config.retention)
    swept = ctx.store.sweep_orphans()
    if swept:
        rows = sum(swept.values())
        log.warning("Removed %d row(s) from %d table(s) left for people erased before the write-time guard", rows,
                    len(swept), extra={"event": "identity.erasure.orphans_swept", "rows": rows, "tables": len(swept)})


def housekeeping_due(ctx: Context, now: float | None = None) -> bool:
    """Whether HOUSEKEEPING_EVERY has passed since housekeeping last ran, in this process or another: the HOUSEKEEPING
    heartbeat in the database, so a restart does not put it off (issue #141). Due at once where it never ran."""
    last = ctx.store.last_beat(HOUSEKEEPING)
    return last is None or (now if now is not None else time.time()) - last >= HOUSEKEEPING_EVERY


def housekeeping_if_due(ctx: Context) -> bool:
    """After every scheduler tick, whatever the tick did (``Publisher.run_forever``): housekeeping when it is due, the
    heartbeat written first, so a run that fails is tried again HOUSEKEEPING_EVERY later and not every tick, and a
    second replica that reads the beat leaves the run to this one. Whether it ran."""
    if not housekeeping_due(ctx):
        return False
    ctx.store.beat(HOUSEKEEPING)
    housekeeping(ctx)
    return True


async def run_scheduler(ctx: Context, stop: asyncio.Event) -> None:
    sync_consumers(ctx)                 # before the first tick: a retired cursor left idle holds nothing (0.1 ms)
    await ctx.publisher.run_forever(ctx.config.scheduler_interval_seconds, stop,
                                    housekeeping=lambda: housekeeping_if_due(ctx),
                                    on_tick=lambda: ctx.store.beat(HEARTBEAT))


SERVICE = ServiceSpec(
    name="SchedulerWorker",
    summary="Publishes scheduled posts when due, retries safely, records heartbeats, purges expired grants",
    kind="worker",
    workers=(run_scheduler,),
)
