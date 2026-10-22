# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #137 (issue #91, F1.10.S3): the retention of domain events and hourly KPIs as settings.

Each test failed on the PR head before the fix committed with it."""

import time
import unittest

from inkcast.libs.config import RetentionPolicy, retention_problems
from inkcast.libs.db import Store
from inkcast.libs.events import DomainEvent, EventSink
from inkcast.libs.kpi import RollupEngine, bucket_start
from inkcast.libs.outbox import OutboxPoller

DAY = 86400


def published(at: float, user: str) -> DomainEvent:
    attrs = {"platform": "devto", "status": "published", "scheduled": False, "lag_seconds": None}
    return DomainEvent("post.published", user_id=user, subject="p", attrs=attrs, at=at)


def counted(store: Store, event: DomainEvent) -> float:
    """How many times ``event`` is in its user's daily bucket (daily buckets are kept whatever the retention)."""
    day = bucket_start(event.at, "day")
    return sum(p.value for p in store.read("posts_published", "day", "user", event.user_id or "", day, day + 1))


def folded_when_it_happened(store: Store, *events: DomainEvent) -> None:
    """``kpi_applied.at`` is the time of folding: date it back to the event, as if folded when it happened."""
    for e in events:
        store._exec("UPDATE kpi_applied SET at = ? WHERE event_id = ?", e.at, e.id)


class HalfWay(EventSink):
    """The rollup engine with a rule that raises on one event until it is fixed forward: the batch fails half-way,
    after the events before that one were folded, and the cursor stays (docs/runbooks/alerts.md,
    InkcastOutboxConsumerLag: "a bug in a rollup rule fails the same batch forever. Fix forward; replays are safe")."""

    name = "kpi-rollups"

    def __init__(self, engine: RollupEngine, poison: DomainEvent):
        self.engine, self.poison, self.broken = engine, poison, True

    def handle(self, events: list[DomainEvent]) -> None:
        for e in events:
            if self.broken and e.id == self.poison.id:
                raise RuntimeError("a rollup rule raised")
            self.engine.handle([e])


class KpiAppliedOutlivesEveryEventTheOutboxHolds(unittest.TestCase):
    """The PR's guard is that the hourly retention is never shorter than the outbox's, "so every event the outbox can
    still replay has its kpi_applied row" (``purge_kpis``). But the outbox keeps an event any consumer hasn't passed
    however old it is, while ``kpi_applied`` was purged by age alone: an event held past the hourly retention lost the
    record that makes its replay harmless, and was counted twice."""

    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.now = time.time()

    def test_a_batch_failing_half_way_longer_than_the_hourly_retention_is_not_counted_twice_when_fixed(self):
        policy = RetentionPolicy(outbox_days=7, kpi_hourly_days=31)
        self.assertEqual(retention_problems(policy), [], "a policy the configuration accepts")
        first, poison = published(self.now - 32 * DAY, "u1"), published(self.now - 32 * DAY, "u2")
        self.store.enqueue(first, poison)
        sink = HalfWay(RollupEngine(self.store), poison)
        poller = OutboxPoller(self.store, "insights", [sink])
        with self.assertLogs("inkcast.libs.outbox", "ERROR"):
            self.assertEqual(poller.drain(), 0)                         # ``first`` folded, then the batch failed
        self.assertEqual(self.store.cursor("insights"), 0)
        folded_when_it_happened(self.store, first)                     # and it has failed for 32 days since
        self.store.purge_expired(policy)
        self.assertEqual([e.id for e in self.store.poll("insights")], [first.id, poison.id], "the outbox holds both")
        sink.broken = False                                            # the rule is fixed forward
        self.assertEqual(poller.drain(), 2)
        self.assertEqual(counted(self.store, first), 1.0, "the replay counted the event a second time")
        self.assertEqual(counted(self.store, poison), 1.0)

    def test_a_consumer_behind_keeps_an_event_whose_replay_must_still_count_once(self):
        event = published(self.now - 100 * DAY, "u1")
        self.store.enqueue(event)
        self.assertEqual(OutboxPoller(self.store, "insights", [RollupEngine(self.store)]).drain(), 1)
        self.store.advance("webhooks", 0)                              # a second consumer, stopped before its first batch
        folded_when_it_happened(self.store, event)
        self.store.purge_expired(RetentionPolicy())                    # the defaults: 7 and 90 days
        self.assertEqual([e.id for e in self.store.poll("insights")], [], "insights passed it")
        self.assertEqual([e.id for e in self.store.poll("webhooks")], [event.id], "the outbox holds it for webhooks")
        self.store.advance("insights", 0)                              # a replay of insights (a rule fixed forward)
        self.assertEqual(OutboxPoller(self.store, "insights", [RollupEngine(self.store)]).drain(), 1)
        self.assertEqual(counted(self.store, event), 1.0, "the replay counted the event a second time")


if __name__ == "__main__":
    unittest.main()
