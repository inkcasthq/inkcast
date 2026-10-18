# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``/api/v1/insights``: a customer's own numbers. Read-only, ``insights:read``.

* ``GET /api/v1/insights/summary?days=30``: the live state (posts by status and platform, ledger by status)
  from the owning tables, plus the period's rates from rollups (publish success, on-time, sync outcomes).
* ``GET /api/v1/insights/kpis?names=a,b&grain=day&days=30``: time series of catalogued customer KPIs.

Every query is scoped to the caller's user id. The OpenFGA seam: once E3 lands, these routes take
``?scope=workspace:<id>`` and call ``authz.require(principal, "can_view_usage", scope)`` before reading
that scope's rows; team KPIs (``member_*``) additionally need the workspace scope, never a user one.
"""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.routing import Route

from ...libs.auth import Principal
from ...libs.contracts.models import InsightsSummary, KpiPoint, KpiSeries, RateSummary
from ...libs.db import Store
from ...libs.http.rest import ApiError, Authed, query_int
from ...libs.kpi import BY_NAME, GRAIN_SECONDS, bucket_start, visible_to_customers

MAX_DAYS = 366
MAX_HOURLY_DAYS = 31  # hourly series beyond a month are thousands of points per KPI: use grain=day


class InsightsReader:
    def __init__(self, store: Store):
        self.store = store

    def _window(self, days: int) -> tuple[float, float]:
        end = bucket_start(time.time(), "day") + 86400
        return end - days * 86400, end

    def _total(self, kpi: str, scope_kind: str, scope_id: str, start: float, end: float) -> dict[str, float]:
        """Per-platform totals of a rollup KPI over the window (daily grain)."""
        out: dict[str, float] = {}
        for p in self.store.read(kpi, "day", scope_kind, scope_id, start, end):
            key = p.dims.get("platform", "all")
            out[key] = out.get(key, 0.0) + p.value
        return out

    def _rate(self, scope_kind: str, scope_id: str, start: float, end: float, kpi: str) -> list[RateSummary]:
        spec = BY_NAME[kpi]
        num = self._total(spec.numerator, scope_kind, scope_id, start, end)
        den: dict[str, float] = {}
        for part in spec.denominator:
            for platform, value in self._total(part, scope_kind, scope_id, start, end).items():
                den[platform] = den.get(platform, 0.0) + value
        return [RateSummary(platform=p, numerator=num.get(p, 0.0), denominator=d, rate=(num.get(p, 0.0) / d) if d else None)
                for p, d in sorted(den.items())]

    def summary(self, user_id: str, days: int) -> InsightsSummary:
        start, end = self._window(days)
        sync = self._total("sync_results", "user", user_id, start, end)
        by_action: dict[str, float] = {}
        for p in self.store.read("sync_results", "day", "user", user_id, start, end):
            by_action[p.dims.get("action", "unknown")] = by_action.get(p.dims.get("action", "unknown"), 0.0) + p.value
        return InsightsSummary(
            days=days, scope="user", scope_id=user_id,
            posts_by_status=self.store.post_status_counts(user_id, start),
            ledger_by_status=self.store.ledger_status_counts(user_id),
            publish_success=self._rate("user", user_id, start, end, "publish_success_rate"),
            on_time=self._rate("user", user_id, start, end, "on_time_rate"),
            sync_by_action={k: int(v) for k, v in sorted(by_action.items())},
            sync_by_platform={k: int(v) for k, v in sorted(sync.items())},
        )

    def series(self, user_id: str, names: list[str], grain: str, days: int) -> list[KpiSeries]:
        if grain not in GRAIN_SECONDS:
            raise ApiError(422, "invalid_request", "grain must be hour or day.")
        if grain == "hour" and days > MAX_HOURLY_DAYS:
            raise ApiError(422, "invalid_request", f"grain=hour covers at most {MAX_HOURLY_DAYS} days; use grain=day.")
        allowed = {k.name: k for k in visible_to_customers() if k.layer == "customer" and k.aggregation == "sum"}
        unknown = [n for n in names if n not in allowed]
        if unknown:
            raise ApiError(422, "invalid_request", f"Unknown or unavailable KPI(s): {', '.join(unknown)}. "
                           f"Available: {', '.join(sorted(allowed))}.")
        start, end = self._window(days)
        return [KpiSeries(name=n, unit=allowed[n].unit, grain=grain,
                          points=[KpiPoint(bucket=p.bucket, dims=p.dims, value=p.value, n=p.n)
                                  for p in self.store.read(n, grain, "user", user_id, start, end)])
                for n in names]


def build_routes(reader: InsightsReader, authed: Authed) -> list[Route]:
    # TODO(E3): authz.require(who, "can_view_usage", scope) once organizations and workspaces exist; until then
    # the scope is the caller's own user id, which the token already proves.

    async def summary(request: Request, who: Principal):
        return reader.summary(who.user_id, query_int(request, "days", 30, MAX_DAYS) or 1)

    async def kpis(request: Request, who: Principal):
        names = [n.strip() for n in request.query_params.get("names", "").split(",") if n.strip()]
        if not names:
            raise ApiError(422, "invalid_request", "names is required, e.g. names=posts_published,posts_failed.")
        if len(names) > 10:
            raise ApiError(422, "invalid_request", "At most 10 KPIs per request.")
        return reader.series(who.user_id, names, request.query_params.get("grain", "day"),
                             query_int(request, "days", 30, MAX_DAYS) or 1)

    return [
        Route("/api/v1/insights/summary", authed("insights:read", summary), methods=["GET"]),
        Route("/api/v1/insights/kpis", authed("insights:read", kpis), methods=["GET"]),
    ]
