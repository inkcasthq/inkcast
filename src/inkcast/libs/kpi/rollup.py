# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The rollup engine: an ``EventSink`` that folds domain events into KPI buckets.

One rule per (event type → KPI): which scopes the event counts for, which dims it keeps, and the value
(usually 1; a lag in seconds for ``schedule_lag_seconds``). Every event is applied to **each scope it
carries** (user now; workspace and organization once E3 fills them in), so a customer's page and a
member's row come from the same write. Team KPIs are the workspace scope with a ``member`` dim, so a
member's number is only ever read in the context of a workspace someone is allowed to see.

Idempotent by event id through ``KpiStore.apply``; a replayed batch is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..events import DomainEvent, EventSink
from .catalogue import BY_NAME, ON_TIME_SECONDS
from .store import Increment, KpiStore

Predicate = Callable[[DomainEvent], bool]
Value = Callable[[DomainEvent], float]


@dataclass(frozen=True)
class Rule:
    event: str
    kpi: str
    dims: tuple[str, ...] = ("platform",)           # attrs copied into dims (missing ones become "unknown")
    when: Predicate | None = None                   # only events that match
    value: Value | None = None                      # default 1.0
    member: bool = False                            # a team KPI: workspace/org scope with dims.member = user_id

    def increments(self, e: DomainEvent) -> list[Increment]:
        if self.when and not self.when(e):
            return []
        value = self.value(e) if self.value else 1.0
        dims = {d: str(e.attrs.get(d, "unknown")) for d in self.dims if d != "member"}
        out: list[Increment] = []
        if self.member:
            for kind, scope in (("workspace", e.workspace_id), ("org", e.org_id)):
                if scope and e.user_id:
                    out.append(Increment(self.kpi, kind, scope, {**dims, "member": e.user_id}, value))
            return out
        for kind, scope in (("user", e.user_id), ("workspace", e.workspace_id), ("org", e.org_id)):
            if scope:
                out.append(Increment(self.kpi, kind, scope, dims, value))
        # application-wide: one row per platform, so the owner's dashboard needs no scan over customers
        out.append(Increment(self.kpi, "platform", dims.get("platform", "all"), dims, value))
        return out


def _scheduled(e: DomainEvent) -> bool:
    return bool(e.attrs.get("scheduled")) and e.attrs.get("lag_seconds") is not None


def _lag(e: DomainEvent) -> float:
    return float(e.attrs.get("lag_seconds") or 0.0)


RULES: tuple[Rule, ...] = (
    Rule("post.published", "posts_published", ("platform", "status")),
    Rule("post.scheduled", "posts_scheduled", when=lambda e: "retry" not in e.attrs),
    Rule("post.scheduled", "posts_retried", when=lambda e: "retry" in e.attrs),
    Rule("post.failed", "posts_failed", ("platform", "reason")),
    Rule("post.unconfirmed", "posts_unconfirmed"),
    Rule("post.recovered", "posts_recovered", ("platform", "status")),
    Rule("post.cancelled", "posts_cancelled"),
    Rule("post.published", "posts_on_time", when=lambda e: _scheduled(e) and _lag(e) <= ON_TIME_SECONDS),
    Rule("post.published", "posts_late", when=lambda e: _scheduled(e) and _lag(e) > ON_TIME_SECONDS),
    Rule("post.published", "schedule_lag_seconds", when=_scheduled, value=_lag),
    Rule("sync.result", "sync_results", ("platform", "action")),
    Rule("post.published", "member_posts_published", ("member", "platform"), member=True),
    Rule("sync.result", "member_sync_results", ("member", "action"), member=True),
)


class RollupEngine(EventSink):
    name = "kpi-rollups"

    def __init__(self, store: KpiStore, rules: tuple[Rule, ...] = RULES, metrics: Any = None):
        self.store, self.metrics = store, metrics
        self.rules: dict[str, list[Rule]] = {}
        for rule in rules:
            if rule.kpi not in BY_NAME:
                raise ValueError(f"rule for unknown KPI {rule.kpi!r}")
            self.rules.setdefault(rule.event, []).append(rule)

    def handle(self, events: list[DomainEvent]) -> None:
        for e in events:
            increments = [inc for rule in self.rules.get(e.type, ()) for inc in rule.increments(e)]
            if not increments:
                continue
            applied = self.store.apply(e.id, e.at, increments)
            if self.metrics is not None:
                self.metrics.inc("inkcast_kpi_events_total", result="applied" if applied else "duplicate")


__all__ = ["RULES", "RollupEngine", "Rule"]
