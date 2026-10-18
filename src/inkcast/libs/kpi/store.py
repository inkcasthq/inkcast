# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``KpiStore``: where rollups live. SQL today (``libs/db/kpi.py``, SQLite now, PostgreSQL in E3); a
columnar store (ClickHouse) later behind the same four methods, when hourly rows per scope outgrow SQL.

A rollup row is one number: ``(kpi, grain, bucket, scope_kind, scope_id, dims) -> (value, n)``. ``grain`` is
``hour`` or ``day``, ``bucket`` the start of that period in UTC epoch seconds, ``scope_kind`` ``user``,
``workspace`` or ``org`` (``platform`` for application-wide numbers), ``dims`` a sorted JSON object of the
small closed-set labels the KPI keeps. ``value`` is the sum, ``n`` the number of events behind it, so a mean
is ``value / n`` and a ratio is computed at read time from two KPIs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Grain = Literal["hour", "day"]
GRAIN_SECONDS: dict[str, int] = {"hour": 3600, "day": 86400}


def bucket_start(at: float, grain: str) -> int:
    size = GRAIN_SECONDS[grain]
    return int(at // size) * size


@dataclass(frozen=True)
class Increment:
    kpi: str
    scope_kind: str
    scope_id: str
    dims: dict[str, Any] = field(default_factory=dict)
    value: float = 1.0


@dataclass(frozen=True)
class Point:
    bucket: int
    dims: dict[str, Any]
    value: float
    n: int


class KpiStore(ABC):
    @abstractmethod
    def apply(self, event_id: str, at: float, increments: list[Increment]) -> bool:
        """Add every increment to its hourly and daily bucket, atomically with a record of ``event_id``.
        False (and nothing written) when the event was applied before: the idempotency that makes
        outbox replays harmless."""

    @abstractmethod
    def read(self, kpi: str, grain: str, scope_kind: str, scope_id: str, start: float, end: float,
             dims: dict[str, Any] | None = None) -> list[Point]:
        """Buckets in ``[start, end)`` for one scope, optionally only rows whose dims include ``dims``."""

    @abstractmethod
    def distinct_scopes(self, kpi: str, scope_kind: str, start: float, end: float) -> int:
        """How many scopes have any value for ``kpi`` in the period (``active_publishers``)."""

    @abstractmethod
    def purge_kpis(self, hourly_keep_seconds: float) -> int:
        """Drop hourly buckets older than the retention; daily buckets are kept."""


__all__ = ["GRAIN_SECONDS", "Grain", "Increment", "KpiStore", "Point", "bucket_start"]
