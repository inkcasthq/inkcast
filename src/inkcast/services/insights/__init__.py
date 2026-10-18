# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""InsightsService: KPIs for customers and operators, from the same codebase.

Two parts, one process when run alone (``INKCAST_APP_NAME=InsightsService``) or inside ``All``:

* a **worker**: the ``insights`` outbox consumer, which folds domain events into KPI rollups
  (``libs/kpi.RollupEngine``) a few seconds after they happen;
* **routes**: ``/api/v1/insights/*``, read-only, scoped to the caller (``insights:read``).

Why rollups live here and not in the services that own the data: one consumer holds the bucket and
idempotency logic, the write path of PublishingApi and SchedulerWorker stays a write path (no analytics
work in a request or a tick), cross-domain KPIs (approval cycle time joins posts and approvals) need
one place anyway, and the ``KpiStore`` can move to a columnar store without touching producers. The
price is eventual consistency of a few seconds, which a KPI page accepts.

Authorization: today every number is scoped by ``user_id``. With E3, ``/insights`` takes an organization
or workspace and ``authz.require(can_view_usage)`` decides (see the TODO seam in ``rest.py``).
"""

from __future__ import annotations

import asyncio
import time

from ...libs.contracts.routes import ROUTES
from ...libs.http.rest import Authed
from ...libs.kpi import RollupEngine
from ...libs.outbox import OutboxPoller
from ...libs.runtime import Context
from ...libs.service import ServiceSpec
from .rest import InsightsReader, build_routes

CONSUMER = "insights"


def poller(ctx: Context) -> OutboxPoller:
    if "insights_poller" not in ctx.extras:
        ctx.extras["insights_poller"] = OutboxPoller(ctx.store, CONSUMER, [RollupEngine(ctx.store, metrics=ctx.metrics)],
                                                     metrics=ctx.metrics)
    return ctx.extras["insights_poller"]


async def run_consumer(ctx: Context, stop: asyncio.Event) -> None:
    await poller(ctx).run_forever(ctx.config.insights_interval_seconds, stop)


def _routes(ctx: Context):
    poller(ctx)  # so a test or an admin can drain on demand through ctx.extras
    return build_routes(InsightsReader(ctx.store), Authed(ctx.verifier, ctx.config.public_url))


def _consumer_alive(ctx: Context) -> str | None:
    p = ctx.extras.get("insights_poller")
    if p is None or not p.last_tick:
        return None  # not started yet (startup) or routes only
    if time.time() - p.last_tick > max(120.0, 6 * ctx.config.insights_interval_seconds):
        return "outbox consumer: no tick recently"
    return None


SERVICE = ServiceSpec(
    name="InsightsService",
    summary="KPIs for customers and operators: folds domain events into rollups, serves /api/v1/insights",
    routes=_routes,
    workers=(run_consumer,),
    public_paths=ROUTES["InsightsService"],
    ready_checks=(_consumer_alive,),
)
