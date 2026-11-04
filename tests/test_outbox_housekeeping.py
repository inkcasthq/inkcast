# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Outbox housekeeping (issue #141).

* ``purge_outbox`` deletes what every cursor row has passed, and nothing removed a cursor row: a consumer retired from
  the code held the purge forever. Housekeeping now makes the cursor table match ``CONSUMERS`` (``sync_consumers``).
* A consumer that had not run yet had no cursor row, so the purge did not wait for it. It gets one at 0, where its
  first poll starts anyway.
* Housekeeping ran on every 90th successful tick: a scheduler restarting more often, or whose ticks kept failing,
  never purged. It now runs every ``HOUSEKEEPING_EVERY``, timed by a heartbeat in the database, after every tick.
* Design §6 said a person's events are "purged within INKCAST_OUTBOX_RETENTION_DAYS (at most 30)": ``erase_user``
  deletes them at once, and a consumer behind keeps the rest however old.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from fakes import seed_people

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config, RetentionPolicy
from inkcast.libs.db import Store
from inkcast.libs.events import CONSUMERS, DomainEvent
from inkcast.libs.observability import EVENTS
from inkcast.libs.outbox import OutboxPoller
from inkcast.libs.pipeline import Publisher
from inkcast.services import scheduler
from inkcast.services.scheduler import (
    HOUSEKEEPING, HOUSEKEEPING_EVERY, housekeeping, housekeeping_due, housekeeping_if_due, run_scheduler,
)

ROOT = Path(__file__).resolve().parents[1]
DESIGN = "docs/design/observability-and-kpis.md"
SECTION_6 = "6. Retention, privacy and GDPR"
DAY = 86400.0
KEY = "k" * 40
TWO_DAYS = RetentionPolicy(outbox_days=2, kpi_hourly_days=31)


def published(at: float, user: str = "u1") -> DomainEvent:
    return DomainEvent("post.published", user_id=user, subject="p", at=at,
                       attrs={"platform": "devto", "status": "published", "scheduled": False, "lag_seconds": None})


def context(store: Store, retention: RetentionPolicy = RetentionPolicy()) -> types.SimpleNamespace:
    """What ``housekeeping`` reads of a Context."""
    return types.SimpleNamespace(store=store, config=types.SimpleNamespace(retention=retention))


class TheCursorsMatchTheCode(unittest.TestCase):
    """``sync_consumers``: a cursor under a name the code no longer has goes; a listed name without one gets one at 0."""

    def setUp(self):
        self.store = Store(":memory:", KEY)
        self.addCleanup(self.store.close)
        seed_people(self.store, "u1")
        self.store.enqueue(*(published(time.time() - 8 * DAY) for _ in range(3)))     # seq 1..3, past 7 days
        self.store.advance("insights", 3)                                             # the live consumer passed them

    def seqs(self, consumer: str) -> list[int]:
        return [e.seq for e in self.store.poll(consumer)]

    def test_a_retired_consumers_cursor_no_longer_holds_the_purge(self):
        self.store.advance("legacy", 1)                     # a consumer the code no longer has, stopped at seq 1
        self.assertEqual(self.store.purge_outbox(7 * DAY), 1, "on main: only what the retired cursor had passed")
        self.assertEqual(self.store.sync_consumers(CONSUMERS), (["legacy"], []))
        self.assertEqual(self.store.purge_outbox(7 * DAY), 2, "the rest, held by nobody live")
        self.assertNotIn("legacy", self.store.outbox_lag(), "no series for a consumer that is gone")
        self.assertEqual(self.store.sync_consumers(CONSUMERS), ([], []), "nothing to do the second time")

    def test_a_live_consumer_behind_keeps_everything_it_has_not_passed(self):
        self.store.advance("insights", 1)                   # behind: has passed seq 1 only
        self.assertEqual(self.store.sync_consumers(CONSUMERS), ([], []), "a listed consumer's cursor stays as it is")
        self.assertEqual(self.store.purge_outbox(7 * DAY), 1)
        self.assertEqual(self.seqs("insights"), [2, 3], "8 days old, held for the consumer that has not passed them")

    def test_a_consumer_not_run_yet_is_registered_before_the_purge_can_pass_what_it_has_not_seen(self):
        with mock.patch.dict(CONSUMERS, {"webhooks": "WebhooksService"}):
            self.assertEqual(self.store.sync_consumers(CONSUMERS), ([], ["webhooks"]))
            self.assertEqual(self.store.purge_outbox(7 * DAY), 0, "on main: 3, the new consumer being invisible")
        self.assertEqual(self.store.cursor("webhooks"), 0)
        self.assertEqual(self.seqs("webhooks"), [1, 2, 3], "its first poll starts where it would have anyway")
        self.assertEqual(self.store.outbox_lag()["webhooks"][0], 3, "and its lag is measured from the start")
        self.assertEqual(OutboxPoller(self.store, "webhooks", []).drain(), 3)
        self.assertEqual(self.store.purge_outbox(7 * DAY), 3, "passed by every consumer now")

    def test_the_sync_refuses_an_empty_list_rather_than_retire_everyone(self):
        with self.assertRaisesRegex(ValueError, "at least one consumer"):
            self.store.sync_consumers(())
        self.assertEqual(self.store.cursor("insights"), 3)

    def test_housekeeping_syncs_before_it_purges_and_names_what_changed(self):
        self.store.advance("legacy", 1)
        with mock.patch.dict(CONSUMERS, {"webhooks": "WebhooksService"}):
            with self.assertLogs("inkcast.services.scheduler", level="INFO") as logs:
                housekeeping(context(self.store))
            events = {getattr(r, "event", ""): r for r in logs.records}
            self.assertEqual(events["outbox.consumer.retired"].consumer, "legacy")
            self.assertEqual(events["outbox.consumer.registered"].consumer, "webhooks")
            self.assertEqual(events["outbox.consumer.retired"].levelno, logging.WARNING)
            self.assertEqual(sorted(self.store.outbox_lag()), ["insights", "webhooks"])
            self.assertEqual(self.seqs("webhooks"), [1, 2, 3], "registered at 0 before the purge, so it holds them")
            self.store.advance("webhooks", 3)                                      # it caught up
            with self.assertNoLogs("inkcast.services.scheduler", level="INFO"):    # nothing to retire or register
                housekeeping(context(self.store))
            self.assertEqual(self.seqs("webhooks"), [], "passed by every consumer the code has: purged")

    def test_the_scheduler_syncs_at_its_start_before_the_first_tick(self):
        """A release that retires a consumer: its cursor would otherwise hold the purge, and keep a lag series that
        trips InkcastOutboxConsumerLag, until the first housekeeping, up to HOUSEKEEPING_EVERY later."""
        self.store.advance("legacy", 1)
        ticks = []

        class FakePublisher:
            async def run_forever(self, interval, stop, housekeeping=None, on_tick=None):
                ticks.append(sorted(self_test.store.outbox_lag()))   # what the loop sees from its first tick

        self_test = self
        ctx = context(self.store)
        ctx.publisher, ctx.config.scheduler_interval_seconds = FakePublisher(), 0.001
        with self.assertLogs("inkcast.services.scheduler", level="WARNING") as logs:
            asyncio.run(run_scheduler(ctx, asyncio.Event()))
        self.assertEqual(ticks, [["insights"]], "gone before the loop started")
        self.assertEqual([getattr(r, "consumer", "") for r in logs.records], ["legacy"])
        self.assertEqual(self.store.purge_outbox(7 * DAY), 3)

    def test_the_catalogue_names_every_consumer_a_service_runs_and_nothing_else(self):
        """Both ways, like EVENT_TYPES: a service that polls the outbox says so in ``CONSUMER`` next to its
        ``ServiceSpec``, and ``CONSUMERS`` maps that name to the service, so housekeeping never retires a live one."""
        found = {}
        for init in sorted((ROOT / "src" / "inkcast" / "services").glob("*/__init__.py")):
            source = init.read_text(encoding="utf-8")
            if "OutboxPoller(" not in source:
                continue
            (name,) = re.findall(r'^CONSUMER = "([a-z_]+)"', source, re.MULTILINE)
            (service,) = re.findall(r'^SERVICE = ServiceSpec\(\s*name="(\w+)"', source, re.MULTILINE)
            found[name] = service
        self.assertEqual(found, dict(CONSUMERS))
        self.assertEqual(found, {"insights": "InsightsService"}, "the one consumer today")
        for event in ("outbox.consumer.retired", "outbox.consumer.registered", "scheduler.housekeeping.failed"):
            self.assertIn(event, EVENTS)


class HousekeepingRunsOnItsOwnClock(unittest.TestCase):
    """After every tick, whatever the tick did; every HOUSEKEEPING_EVERY, timed by a heartbeat in the database."""

    def store(self, path: str = ":memory:") -> Store:
        store = Store(path, KEY)
        self.addCleanup(store.close)
        seed_people(store, "u1")
        return store

    def seed(self, store: Store) -> tuple[DomainEvent, DomainEvent]:
        """An event 3 days old and one from today, both passed by every consumer: a 2-day retention purges the first."""
        old, young = published(time.time() - 3 * DAY), published(time.time())
        store.enqueue(old, young)
        store.advance("insights", 2)
        return old, young

    def test_it_runs_while_every_tick_fails(self):
        cfg = Config(public_url="http://localhost:8787", secret_key=KEY, database_path=":memory:", login="password",
                     password="p" * 12, retention=TWO_DAYS, scheduler_interval_seconds=0.001)
        ctx = build(cfg, resolve("SchedulerWorker"))
        self.addCleanup(ctx.store.close)
        seed_people(ctx.store, "u1")
        old, young = self.seed(ctx.store)

        class Failing(Publisher):
            async def run_due(self, now=None):
                raise RuntimeError("storage is broken for the tick")

        ctx.publisher = Failing(ctx.store, {})

        async def run():
            stop = asyncio.Event()
            task = asyncio.create_task(run_scheduler(ctx, stop))
            deadline = time.monotonic() + 30
            while [e.id for e in ctx.store.poll("audit")] != [young.id] and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            stop.set()
            await asyncio.wait_for(task, 10)

        with self.assertLogs("inkcast.libs.pipeline", level="ERROR") as logs:
            asyncio.run(run())
        self.assertEqual([e.id for e in ctx.store.poll("audit")], [young.id], "purged although no tick ever succeeded")
        self.assertIn("scheduler.tick.failed", {getattr(r, "event", "") for r in logs.records})
        self.assertIsNotNone(ctx.store.last_beat(HOUSEKEEPING), "the run is on record, for the next process")

    def test_once_per_interval_timed_in_the_database_so_a_restart_does_not_put_it_off(self):
        path = str(Path(tempfile.mkdtemp(prefix="ob-141-")) / "inkcast.db")
        first = self.store(path)
        old, young = self.seed(first)
        ctx = context(first, TWO_DAYS)
        self.assertTrue(housekeeping_due(ctx), "never ran: due at once")
        self.assertTrue(housekeeping_if_due(ctx))
        self.assertEqual([e.id for e in first.poll("audit")], [young.id])
        self.assertFalse(housekeeping_due(ctx), "ran just now")
        self.assertFalse(housekeeping_if_due(ctx))
        restarted = context(self.store(path), TWO_DAYS)                 # another process, or this one after a restart
        self.assertFalse(housekeeping_due(restarted), "the clock is the database's, not the process's")
        self.assertTrue(housekeeping_due(restarted, now=time.time() + HOUSEKEEPING_EVERY), "and it comes round")
        first.beat(HOUSEKEEPING, time.time() - HOUSEKEEPING_EVERY)
        self.assertTrue(housekeeping_if_due(restarted))
        self.assertEqual(HOUSEKEEPING_EVERY, 30 * 60.0, "what 90 ticks of the default 20 s loop were")

    def test_a_failing_run_costs_no_tick_and_is_tried_again_after_the_interval_not_every_tick(self):
        store = self.store()
        old, young = self.seed(store)
        ctx = context(store, TWO_DAYS)
        calls = []

        def broken(ctx):
            calls.append(time.time())
            raise RuntimeError("the purge failed")

        with mock.patch.object(scheduler, "housekeeping", broken):
            with self.assertRaisesRegex(RuntimeError, "the purge failed"):
                housekeeping_if_due(ctx)                                    # claimed the slot, then failed: the loop logs it
            self.assertEqual(len(calls), 1)
            self.assertFalse(housekeeping_if_due(ctx), "not again every tick: the slot is taken until it comes round")
            self.assertEqual(len(calls), 1)
        self.assertIsNotNone(store.last_beat(HOUSEKEEPING))
        self.assertEqual([e.id for e in store.poll("audit")], [old.id, young.id], "nothing purged by the failed run")

        publisher = Publisher(store, {})
        ticks = []

        async def run():
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, stop.set)
            await publisher.run_forever(0.001, stop, housekeeping=lambda: 1 / 0, on_tick=lambda: ticks.append(1))

        with self.assertLogs("inkcast.libs.pipeline", level="ERROR") as logs:
            asyncio.run(run())
        self.assertGreater(len(ticks), 0, "the ticks went on")
        self.assertGreater(publisher.last_tick, 0.0)
        failed = [r for r in logs.records if getattr(r, "event", "") == "scheduler.housekeeping.failed"]
        self.assertEqual(len(failed), len(ticks), "called after every tick, and its failure is its own")
        self.assertNotIn("scheduler.tick.failed", {getattr(r, "event", "") for r in logs.records})


class TheDesignSaysWhatHappens(unittest.TestCase):
    def text(self, path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def section(self, path: str, heading: str) -> str:
        body = self.text(path)
        start = body.index(f"\n## {heading}\n")
        end = body.find("\n## ", start + 1)
        return body[start:end if end != -1 else None]

    def test_section_6_says_a_persons_events_go_with_the_erasure_and_the_rest_wait_for_every_consumer(self):
        """It said "purged within INKCAST_OUTBOX_RETENTION_DAYS (7 by default, at most 30) by retention": ``erase_user``
        deletes the rows that name the person in its own transaction (``USER_TABLES`` in libs/db/erasure.py), and a
        consumer behind keeps the other rows past 30 days."""
        retention = self.section(DESIGN, SECTION_6)
        self.assertNotIn("purged within", retention)
        self.assertNotIn("at most 30)", retention)
        self.assertIn("erase_user", retention)
        self.assertIn("consumer behind", retention)

    def test_nothing_says_housekeeping_runs_every_90_ticks_any_more(self):
        paths = [p for folder in ("src", "docs") for p in (ROOT / folder).rglob("*") if p.suffix in (".py", ".md")]
        paths = [p for p in paths if not str(p).startswith(str(ROOT / "docs" / "backlog"))] + [ROOT / "README.md"]
        stale = [str(p.relative_to(ROOT)) for p in paths
                 if re.search(r"every 90 ticks|90th tick|% 90 ==", p.read_text(encoding="utf-8"))]
        self.assertEqual(stale, [])
        self.assertIn("HOUSEKEEPING_EVERY", self.section(DESIGN, SECTION_6))
        self.assertIn("30 minutes", self.section("docs/runbooks/alerts.md", "InkcastDataVolumeNearlyFull"))


if __name__ == "__main__":
    unittest.main()
