# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""KPI rollups in SQL (owned by InsightsService): the ``KpiStore`` on the shared database.

``kpi_rollups`` holds one row per (kpi, grain, bucket, scope, dims); ``kpi_applied`` remembers every
event id folded in, in the same transaction, so a replayed outbox batch changes nothing.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..kpi.store import GRAIN_SECONDS, Increment, KpiStore, Point, bucket_start
from .database import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS kpi_rollups (
  kpi TEXT NOT NULL, grain TEXT NOT NULL, bucket INTEGER NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
  dims TEXT NOT NULL, value REAL NOT NULL, n INTEGER NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY (kpi, grain, bucket, scope_kind, scope_id, dims)
);
CREATE INDEX IF NOT EXISTS kpi_rollups_scope ON kpi_rollups(scope_kind, scope_id, kpi, grain, bucket);
CREATE TABLE IF NOT EXISTS kpi_applied (event_id TEXT PRIMARY KEY, at REAL NOT NULL);
"""


class KpiRepo(Database, KpiStore):
    def apply(self, event_id: str, at: float, increments: list[Increment]) -> bool:
        now = time.time()
        with self._tx() as c:
            if c.execute("SELECT 1 FROM kpi_applied WHERE event_id = ?", (event_id,)).fetchone():
                return False
            c.execute("INSERT INTO kpi_applied VALUES (?, ?)", (event_id, now))
            for inc in increments:
                dims = json.dumps(inc.dims, sort_keys=True)
                for grain in GRAIN_SECONDS:
                    c.execute(
                        "INSERT INTO kpi_rollups (kpi, grain, bucket, scope_kind, scope_id, dims, value, n, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)"
                        " ON CONFLICT (kpi, grain, bucket, scope_kind, scope_id, dims)"
                        " DO UPDATE SET value = value + excluded.value, n = n + 1, updated_at = excluded.updated_at",
                        (inc.kpi, grain, bucket_start(at, grain), inc.scope_kind, inc.scope_id, dims, inc.value, now),
                    )
        return True

    def read(self, kpi: str, grain: str, scope_kind: str, scope_id: str, start: float, end: float,
             dims: dict[str, Any] | None = None) -> list[Point]:
        rows = self._all(
            "SELECT bucket, dims, value, n FROM kpi_rollups WHERE kpi = ? AND grain = ? AND scope_kind = ? AND scope_id = ?"
            " AND bucket >= ? AND bucket < ? ORDER BY bucket, dims", kpi, grain, scope_kind, scope_id, int(start), int(end))
        out = []
        for r in rows:
            d = json.loads(r["dims"])
            if dims and any(d.get(k) != v for k, v in dims.items()):
                continue
            out.append(Point(int(r["bucket"]), d, float(r["value"]), int(r["n"])))
        return out

    def distinct_scopes(self, kpi: str, scope_kind: str, start: float, end: float) -> int:
        row = self._one("SELECT COUNT(DISTINCT scope_id) AS n FROM kpi_rollups WHERE kpi = ? AND grain = 'day'"
                        " AND scope_kind = ? AND bucket >= ? AND bucket < ?", kpi, scope_kind, int(start), int(end))
        return int(row["n"]) if row else 0

    def purge_kpis(self, hourly_keep_seconds: float) -> int:
        """Drop hourly buckets older than ``hourly_keep_seconds`` (``Config.retention.kpi_hourly_seconds``) and the
        ``kpi_applied`` rows from before the same cutoff; daily buckets are kept. The retention is the caller's, and it
        is never shorter than the outbox's (the configuration refuses that), so every event the outbox can still
        replay has its ``kpi_applied`` row."""
        cutoff = int(time.time() - hourly_keep_seconds)
        n = self._exec("DELETE FROM kpi_rollups WHERE grain = 'hour' AND bucket < ?", cutoff)
        self._exec("DELETE FROM kpi_applied WHERE at < ?", cutoff)
        return n


__all__ = ["KpiRepo", "SCHEMA"]
