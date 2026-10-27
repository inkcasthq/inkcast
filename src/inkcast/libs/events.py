# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Domain events: what happened, as facts other parts of Inkcast consume.

A ``DomainEvent`` is written in the **same transaction** as the state change it describes (the
transactional outbox, ``libs/db/outbox.py``): a post that became ``published`` always has its
``post.published`` event, and never twice. Consumers read the outbox in order through an
``OutboxPoller`` (one cursor per consumer) and hand each batch to ``EventSink``s:

* today: the KPI rollup engine (``libs/kpi``), run by ``InsightsService``;
* later: outbound webhooks (E7) and a broker (NATS/Kafka) when another system needs the stream.

Every sink is idempotent by ``event.id``: a crash between "handled" and "cursor advanced" replays the
batch, which must be harmless. ``EVENT_TYPES`` is the catalogue: a test fails on an event type that is
emitted but not listed, or listed but never emitted, so dashboards and rollups can rely on the names.

Scope fields (``user_id``, ``org_id``, ``workspace_id``) are what per-customer KPIs group by. Only
``user_id`` is filled today; E3 adds the organization and workspace without changing consumers.
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

EVENT_TYPES: dict[str, str] = {
    "post.scheduled": "A post was queued for a future time (a new post, or a retry re-queued it).",
    "post.published": "A platform accepted a post (attrs.status: published, draft or unlisted).",
    "post.failed": "A post failed for good (attrs.reason) and waits for a person.",
    "post.unconfirmed": "A platform may or may not have published a post; a person must check.",
    "post.recovered": "A post interrupted mid-send was resolved after a restart (attrs.status).",
    "post.cancelled": "A scheduled post was cancelled before it was sent, or a recovering or unconfirmed one by the person "
                      "(its send was interrupted, or the platform had not said whether it arrived).",
    "post.resent": "A person resent an unconfirmed post as a new one (attrs.new_post_id); the old one is cancelled. "
                   "user_id is who decided.",
    "sync.result": "One (file, platform) outcome of a Git publishing sync (attrs.action).",
}


@dataclass(frozen=True)
class DomainEvent:
    type: str
    user_id: str | None = None
    org_id: str | None = None
    workspace_id: str | None = None
    subject: str | None = None  # the post id, the ledger key...
    attrs: dict[str, Any] = field(default_factory=dict)  # small, JSON-serialisable, never a secret
    at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: "evt_" + uuid.uuid4().hex)
    seq: int | None = None  # the outbox position, set when read back

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {self.type!r}; add it to libs/events.EVENT_TYPES")

    def to_row(self) -> tuple:
        return (self.id, self.type, self.at, self.user_id, self.org_id, self.workspace_id, self.subject,
                json.dumps(self.attrs, sort_keys=True))

    @classmethod
    def from_row(cls, row: Any) -> "DomainEvent":
        return cls(type=row["type"], user_id=row["user_id"], org_id=row["org_id"], workspace_id=row["workspace_id"],
                   subject=row["subject"], attrs=json.loads(row["attrs"]), at=row["at"], id=row["id"], seq=row["seq"])


def post_event(type: str, row: dict[str, Any], **attrs: Any) -> DomainEvent:
    """An event about a post row: carries the post's scope, platform and id."""
    return DomainEvent(type, user_id=row.get("user_id"), org_id=row.get("org_id"), workspace_id=row.get("workspace_id"),
                       subject=row.get("id"), attrs={"platform": row.get("platform"), **attrs})


class EventSink(ABC):
    """A consumer of outbox batches. ``handle`` must be idempotent by event id."""

    name: str = "sink"

    @abstractmethod
    def handle(self, events: list[DomainEvent]) -> None: ...


class MemorySink(EventSink):
    """Collects events in a list (tests, and the simplest way to see what a flow emits)."""

    name = "memory"

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    def handle(self, events: list[DomainEvent]) -> None:
        self.events.extend(events)


__all__ = ["DomainEvent", "EVENT_TYPES", "EventSink", "MemorySink", "post_event"]
