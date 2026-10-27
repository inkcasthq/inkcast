# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""SchedulerWorker: sends scheduled posts when they are due, and keeps the stores tidy.

It drains the posts queue (``claim_due_posts`` is atomic, so replicas never double-send),
retries only failures that certainly didn't publish, records a heartbeat every tick (visible to
every process through the store), and purges expired grants and tickets. It serves no HTTP
except probes and /metrics on the metrics port. Overdue posts show up as the
``inkcast_posts_overdue`` gauge, which the InkcastPostsOverdue alert watches.
"""

from __future__ import annotations

import asyncio

from ...libs.runtime import Context
from ...libs.service import ServiceSpec

HEARTBEAT = "scheduler"


def housekeeping(ctx: Context) -> None:
    """Every 90th tick: expired grants and tickets, then domain events and hourly KPI buckets past the configured
    retention (INKCAST_OUTBOX_RETENTION_DAYS, INKCAST_KPI_HOURLY_RETENTION_DAYS; F1.10.S3, issue #91)."""
    ctx.store.purge_expired(ctx.config.retention)


async def run_scheduler(ctx: Context, stop: asyncio.Event) -> None:
    await ctx.publisher.run_forever(ctx.config.scheduler_interval_seconds, stop,
                                    housekeeping=lambda: housekeeping(ctx), on_tick=lambda: ctx.store.beat(HEARTBEAT))


SERVICE = ServiceSpec(
    name="SchedulerWorker",
    summary="Publishes scheduled posts when due, retries safely, records heartbeats, purges expired grants",
    kind="worker",
    workers=(run_scheduler,),
)
