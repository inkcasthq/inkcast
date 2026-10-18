# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""KPIs: the catalogue as code, the rollup engine (domain events → hourly and daily buckets per scope)
and the ``KpiStore`` seam. See docs/design/observability-and-kpis.md."""

from __future__ import annotations

from .catalogue import BY_NAME, CATALOGUE, ON_TIME_SECONDS, Kpi, built, visible_to_customers
from .rollup import RULES, RollupEngine, Rule
from .store import GRAIN_SECONDS, Increment, KpiStore, Point, bucket_start

__all__ = ["BY_NAME", "CATALOGUE", "GRAIN_SECONDS", "Increment", "Kpi", "KpiStore", "ON_TIME_SECONDS", "Point", "RULES",
           "RollupEngine", "Rule", "bucket_start", "built", "visible_to_customers"]
