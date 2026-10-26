# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""F1.10.S3 (issue #91): retention is a setting.

``INKCAST_OUTBOX_RETENTION_DAYS`` (7, 1-30) keeps domain events every consumer has passed; ``INKCAST_KPI_HOURLY_RETENTION_DAYS``
(90, 31-400) keeps hourly KPI buckets and the ``kpi_applied`` rows that stop a replayed event counting twice. The hourly
retention is at least 31 days because ``grain=hour`` serves up to 31 days, and never shorter than the outbox's, because
``kpi_applied`` must outlive the events it makes harmless. The scheduler's housekeeping hands the configured policy to
``Store.purge_expired``; the defaults are exactly what was hard-coded before (observability design §6)."""

import asyncio
import dataclasses
import time
import unittest
from pathlib import Path

import yaml
from fakes import seed_people

from inkcast.launcher import build, resolve
from inkcast.libs.config import (
    KPI_HOURLY_RETENTION_DAYS, OUTBOX_RETENTION_DAYS, Config, ConfigError, RetentionPolicy, load_config, retention_problems,
)
from inkcast.libs.db import Store
from inkcast.libs.events import DomainEvent
from inkcast.libs.kpi import RollupEngine, bucket_start
from inkcast.libs.observability import Metrics
from inkcast.libs.outbox import OutboxPoller
from inkcast.services.insights.rest import MAX_HOURLY_DAYS
from inkcast.services.scheduler import housekeeping, run_scheduler

ROOT = Path(__file__).resolve().parents[1]
BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
        "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
OUTBOX, HOURLY = "INKCAST_OUTBOX_RETENTION_DAYS", "INKCAST_KPI_HOURLY_RETENTION_DAYS"
DAY = 86400
# Before this story: purge_outbox(keep_seconds=7 * 86400) and purge_kpis(hourly_keep_seconds=90 * 86400), called with
# their defaults by Store.purge_expired.
TODAY = RetentionPolicy(outbox_days=7, kpi_hourly_days=90)


def config(**env) -> Config:
    return load_config({**BASE, **env})


def by_hand(**fields) -> Config:
    return Config(**{"public_url": "http://localhost:8787", "secret_key": "k" * 40, "database_path": ":memory:",
                     "login": "password", "password": "p" * 12, **fields})


def published(at: float, user: str = "u1", platform: str = "devto") -> DomainEvent:
    attrs = {"platform": platform, "status": "published", "scheduled": False, "lag_seconds": None}
    return DomainEvent("post.published", user_id=user, subject="p", attrs=attrs, at=at)


# ---- the settings at load --------------------------------------------------------------------------------


class OutboxRetentionDays(unittest.TestCase):
    def test_from_1_to_30_and_the_default_is_7(self):
        self.assertEqual(config().retention.outbox_days, 7)
        self.assertEqual(config(INKCAST_OUTBOX_RETENTION_DAYS="").retention.outbox_days, 7)
        for raw, value in (("1", 1), (" 14 ", 14), ("30", 30)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_OUTBOX_RETENTION_DAYS=raw)
                self.assertEqual(cfg.retention.outbox_days, value)
                self.assertEqual(cfg.retention.outbox_seconds, value * DAY)
                cfg.validate()

    def test_outside_the_range_or_not_a_whole_number_is_a_configuration_error_naming_the_variable(self):
        for raw in ("0", "31", "-7", "365", "7.5", "seven", "1e1", "0x7", "7,14", "inf", "true", "off"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{OUTBOX} must be a whole number of days from 1 to 30") as caught:
                    config(INKCAST_OUTBOX_RETENTION_DAYS=raw)
                message = str(caught.exception)
                self.assertIn(repr(raw), message, "the error quotes the value it refused")
                self.assertNotIn(HOURLY, message, "only the variable at fault is named")


class KpiHourlyRetentionDays(unittest.TestCase):
    def test_from_31_to_400_and_the_default_is_90(self):
        self.assertEqual(config().retention.kpi_hourly_days, 90)
        self.assertEqual(config(INKCAST_KPI_HOURLY_RETENTION_DAYS="").retention.kpi_hourly_days, 90)
        for raw, value in (("31", 31), (" 180 ", 180), ("400", 400)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_KPI_HOURLY_RETENTION_DAYS=raw)
                self.assertEqual(cfg.retention.kpi_hourly_days, value)
                self.assertEqual(cfg.retention.kpi_hourly_seconds, value * DAY)
                cfg.validate()

    def test_outside_the_range_or_not_a_whole_number_is_a_configuration_error_naming_the_variable(self):
        for raw in ("30", "0", "401", "-90", "1000", "90.5", "ninety", "1e2", "0x5a", "90,180", "nan", "false", "off"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{HOURLY} must be a whole number of days from 31 to 400") as caught:
                    config(INKCAST_KPI_HOURLY_RETENTION_DAYS=raw)
                message = str(caught.exception)
                self.assertIn(repr(raw), message, "the error quotes the value it refused")
                self.assertNotIn(f"{OUTBOX} must", message, "only the variable at fault is named")

    def test_the_floor_is_what_grain_hour_serves(self):
        """``GET /api/v1/insights/kpis?grain=hour&days=31`` is the longest hourly series: the retention can't be shorter."""
        self.assertEqual(KPI_HOURLY_RETENTION_DAYS[0], MAX_HOURLY_DAYS)
        self.assertEqual(KPI_HOURLY_RETENTION_DAYS[0], 31)
        with self.assertRaisesRegex(ConfigError, f"{HOURLY} must"):
            config(INKCAST_KPI_HOURLY_RETENTION_DAYS=str(MAX_HOURLY_DAYS - 1))
        config(INKCAST_KPI_HOURLY_RETENTION_DAYS=str(MAX_HOURLY_DAYS)).validate()


class TheCrossCheck(unittest.TestCase):
    """The hourly retention may never be shorter than the outbox's: ``kpi_applied`` is purged at the hourly cutoff, and
    it is the record that stops an event replayed from the outbox counting twice."""

    def order(self, policy: RetentionPolicy) -> list[str]:
        return [p for p in retention_problems(policy) if "must not be shorter than" in p]

    def test_an_hourly_retention_shorter_than_the_outbox_is_refused_naming_both(self):
        for outbox, hourly in ((30, 20), (30, 29), (30, 0), (15, 14), (7, 6), (1, 0)):
            with self.subTest(outbox=outbox, hourly=hourly):
                [problem] = self.order(RetentionPolicy(outbox, hourly))
                self.assertIn(f"{HOURLY} ({hourly}) must not be shorter than {OUTBOX} ({outbox})", problem)
                self.assertIn("kpi_applied", problem, "the error says why")
                self.assertIn("counting twice", problem)

    def test_equal_or_longer_passes_the_order(self):
        for outbox, hourly in ((7, 90), (30, 31), (30, 30), (1, 400), (20, 20), (1, 1)):
            with self.subTest(outbox=outbox, hourly=hourly):
                self.assertEqual(self.order(RetentionPolicy(outbox, hourly)), [])

    def test_an_outbox_outside_its_bounds_is_the_problem_not_the_order(self):
        """``INKCAST_OUTBOX_RETENTION_DAYS=365`` with the hourly default (90) is the outbox's fault: the order rule
        would blame the hourly variable, so it is judged only against an outbox retention within its bounds."""
        for outbox, hourly in ((365, 90), (40, 35), (100, 99), (0, 90), (31, 30), ("30", 20), (None, 90)):
            with self.subTest(outbox=outbox, hourly=hourly):
                problems = retention_problems(RetentionPolicy(outbox, hourly))
                self.assertEqual(self.order(RetentionPolicy(outbox, hourly)), [])
                self.assertTrue(any(p.startswith(f"{OUTBOX} must") for p in problems))

    def test_at_load_and_for_a_config_built_by_hand(self):
        with self.assertRaises(ConfigError) as caught:
            config(INKCAST_OUTBOX_RETENTION_DAYS="30", INKCAST_KPI_HOURLY_RETENTION_DAYS="20")
        message = str(caught.exception)
        self.assertIn(f"{HOURLY} (20) must not be shorter than {OUTBOX} (30)", message)
        self.assertIn(f"{HOURLY} must be a whole number of days from 31 to 400", message, "the bound is reported too")
        self.assertNotIn(f"{OUTBOX} must", message, "the outbox value itself is fine")
        with self.assertRaisesRegex(ConfigError, f"{HOURLY} \\(29\\) must not be shorter than {OUTBOX} \\(30\\)"):
            by_hand(retention=RetentionPolicy(30, 29)).validate()

    def test_the_bounds_keep_the_order_on_their_own_and_the_rule_is_stated_anyway(self):
        """Today the ranges don't overlap (31 > 30), so the bounds alone keep ``kpi_applied`` outliving the events; the
        cross-check is the invariant itself, so a change to either bound can't silently lose it."""
        self.assertGreater(KPI_HOURLY_RETENTION_DAYS[0], OUTBOX_RETENTION_DAYS[1])
        self.assertEqual((OUTBOX_RETENTION_DAYS, KPI_HOURLY_RETENTION_DAYS), ((1, 30), (31, 400)))
        config(INKCAST_OUTBOX_RETENTION_DAYS="30", INKCAST_KPI_HOURLY_RETENTION_DAYS="31").validate()   # the tightest gap


class ConfigBuiltByHand(unittest.TestCase):
    def test_validate_applies_the_same_rules(self):
        for policy in (TODAY, RetentionPolicy(1, 31), RetentionPolicy(30, 400), RetentionPolicy(30, 31)):
            with self.subTest(policy=policy):
                by_hand(retention=policy).validate()
        for policy, name in (
            (RetentionPolicy(0, 90), OUTBOX), (RetentionPolicy(31, 90), OUTBOX), (RetentionPolicy(7.0, 90), OUTBOX),
            (RetentionPolicy("7", 90), OUTBOX), (RetentionPolicy(True, 90), OUTBOX), (RetentionPolicy(None, 90), OUTBOX),
            (RetentionPolicy(7, 30), HOURLY), (RetentionPolicy(7, 401), HOURLY), (RetentionPolicy(7, 90.0), HOURLY),
            (RetentionPolicy(7, "90"), HOURLY), (RetentionPolicy(7, False), HOURLY), (RetentionPolicy(7, None), HOURLY),
        ):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ConfigError, f"{name} must be a whole number of days"):
                    by_hand(retention=policy).validate()

    def test_both_wrong_names_both(self):
        with self.assertRaises(ConfigError) as caught:
            config(INKCAST_OUTBOX_RETENTION_DAYS="0", INKCAST_KPI_HOURLY_RETENTION_DAYS="401")
        self.assertIn(f"{OUTBOX} must", str(caught.exception))
        self.assertIn(f"{HOURLY} must", str(caught.exception))

    def test_the_policy_is_a_frozen_value(self):
        policy = dataclasses.replace(TODAY, outbox_days=14)
        self.assertEqual((policy.outbox_days, policy.kpi_hourly_days), (14, 90))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.outbox_days = 1


# ---- the defaults are today's behaviour --------------------------------------------------------------------


class Defaults(unittest.TestCase):
    def test_the_settings_default_to_what_was_hard_coded(self):
        self.assertEqual(RetentionPolicy(), TODAY)
        self.assertEqual(config().retention, TODAY)
        self.assertEqual(by_hand().retention, TODAY)
        self.assertEqual(TODAY.outbox_seconds, 7 * 86400)
        self.assertEqual(TODAY.kpi_hourly_seconds, 90 * 86400)

    def test_purge_expired_without_a_policy_purges_at_7_and_90_days(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        seed_people(store, "u1")
        now = time.time()
        old, young = published(at=now - 8 * DAY), published(at=now - 6 * DAY)
        store.enqueue(old, young)
        store.advance("insights", 2)
        engine = RollupEngine(store)
        stale, fresh = published(at=now - 91 * DAY), published(at=now - 89 * DAY)
        engine.handle([stale, fresh])
        store._exec("UPDATE kpi_applied SET at = ? WHERE event_id = ?", stale.at, stale.id)
        store._exec("UPDATE kpi_applied SET at = ? WHERE event_id = ?", fresh.at, fresh.id)
        store.purge_expired()
        self.assertEqual([e.id for e in store.poll("audit")], [young.id])
        self.assertEqual(hourly_rows(store, stale.at), [])
        self.assertEqual(len(hourly_rows(store, fresh.at)), 1)
        self.assertEqual(applied(store), {fresh.id})


def hourly_rows(store: Store, at: float, user: str = "u1") -> list:
    hour = bucket_start(at, "hour")
    return store.read("posts_published", "hour", "user", user, hour, hour + 1)


def daily_rows(store: Store, at: float, user: str = "u1") -> list:
    day = bucket_start(at, "day")
    return store.read("posts_published", "day", "user", user, day, day + 1)


def applied(store: Store) -> set[str]:
    return {r["event_id"] for r in store._all("SELECT event_id FROM kpi_applied")}


# ---- the configured retention is what the purge applies ----------------------------------------------------


class ConfiguredOutboxRetention(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        seed_people(self.store, "u1")           # a row for a person without a users row is refused (#196)
        self.now = time.time()

    def test_purges_events_every_consumer_passed_at_that_age_and_not_before(self):
        policy = RetentionPolicy(outbox_days=2, kpi_hourly_days=31)
        ages = (3 * DAY, 2 * DAY + 60, 2 * DAY - 60, 1 * DAY, 0)
        events = [published(at=self.now - age) for age in ages]
        self.store.enqueue(*events)
        self.store.advance("insights", len(events))                     # every consumer has passed them all
        self.store.purge_expired(policy)
        kept = [e.id for e in self.store.poll("audit")]
        self.assertEqual(kept, [e.id for e in events[2:]], "older than 2 days is gone; 2 days less a minute stays")

    def test_the_default_policy_keeps_what_a_shorter_one_purges(self):
        events = [published(at=self.now - 3 * DAY), published(at=self.now - 10 * DAY)]
        self.store.enqueue(*events)
        self.store.advance("insights", 2)
        self.store.purge_expired(TODAY)
        self.assertEqual([e.id for e in self.store.poll("audit")], [events[0].id], "3 days is inside the default 7")
        self.store.purge_expired(RetentionPolicy(outbox_days=1, kpi_hourly_days=31))
        self.assertEqual(self.store.poll("audit"), [])

    def test_a_consumer_still_behind_keeps_old_events_whatever_the_retention(self):
        events = [published(at=self.now - 20 * DAY) for _ in range(3)]
        self.store.enqueue(*events)
        self.store.advance("insights", 3)
        self.store.advance("webhooks", 1)                                # has handled only the first
        self.store.purge_expired(RetentionPolicy(outbox_days=1, kpi_hourly_days=31))
        self.assertEqual([e.id for e in self.store.poll("audit")], [e.id for e in events[1:]])

    def test_the_longest_retention_keeps_a_month(self):
        events = [published(at=self.now - 29 * DAY), published(at=self.now - 31 * DAY)]
        self.store.enqueue(*events)
        self.store.advance("insights", 2)
        self.store.purge_expired(RetentionPolicy(outbox_days=30, kpi_hourly_days=400))
        self.assertEqual([e.id for e in self.store.poll("audit")], [events[0].id])


class ConfiguredHourlyRetention(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        seed_people(self.store, "u1")
        self.engine = RollupEngine(self.store)
        self.now = time.time()

    def fold(self, *events: DomainEvent) -> None:
        """Apply the events as if each had been folded in when it happened (kpi_applied.at is the time of folding)."""
        self.engine.handle(list(events))
        for e in events:
            self.store._exec("UPDATE kpi_applied SET at = ? WHERE event_id = ?", e.at, e.id)

    def test_purges_hourly_buckets_and_kpi_applied_at_that_age_and_keeps_daily_buckets(self):
        policy = RetentionPolicy(outbox_days=7, kpi_hourly_days=40)
        old, edge, young = (published(at=self.now - 41 * DAY), published(at=self.now - 40 * DAY + 2 * 3600),
                            published(at=self.now - 20 * DAY))                   # the hour bucket floors at, so an hour's margin
        self.fold(old, edge, young)
        self.assertEqual(applied(self.store), {old.id, edge.id, young.id})
        self.store.purge_expired(policy)
        self.assertEqual(hourly_rows(self.store, old.at), [], "the 41-day-old hour is gone")
        self.assertEqual(len(hourly_rows(self.store, edge.at)), 1, "an hour inside the retention stays")
        self.assertEqual(len(hourly_rows(self.store, young.at)), 1)
        self.assertEqual(applied(self.store), {edge.id, young.id}, "kpi_applied is purged at the same cutoff")
        self.assertEqual(len(daily_rows(self.store, old.at)), 1, "daily buckets are kept whatever the retention")

    def test_the_default_policy_keeps_what_a_shorter_one_purges(self):
        event = published(at=self.now - 60 * DAY)
        self.fold(event)
        self.store.purge_expired(TODAY)
        self.assertEqual(len(hourly_rows(self.store, event.at)), 1, "60 days is inside the default 90")
        self.assertEqual(applied(self.store), {event.id})
        self.store.purge_expired(RetentionPolicy(outbox_days=7, kpi_hourly_days=31))
        self.assertEqual(hourly_rows(self.store, event.at), [])
        self.assertEqual(applied(self.store), set())

    def test_the_longest_retention_keeps_400_days(self):
        kept, gone = published(at=self.now - 399 * DAY), published(at=self.now - 401 * DAY)
        self.fold(kept, gone)
        self.store.purge_expired(RetentionPolicy(outbox_days=30, kpi_hourly_days=400))
        self.assertEqual(len(hourly_rows(self.store, kept.at)), 1)
        self.assertEqual(hourly_rows(self.store, gone.at), [])
        self.assertEqual(applied(self.store), {kept.id})


class ReplayInsideTheOutboxRetention(unittest.TestCase):
    """Why the cross-check exists: an event the outbox still holds can be replayed (a consumer's cursor lost, a sink
    that failed half-way), and only its ``kpi_applied`` row keeps the replay from counting twice."""

    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        seed_people(self.store, "u1")
        self.metrics = Metrics()
        self.metrics.counter("inkcast_kpi_events_total", "")
        self.engine = RollupEngine(self.store, metrics=self.metrics)
        self.poller = OutboxPoller(self.store, "insights", [self.engine])

    def folded_29_days_ago(self) -> DomainEvent:
        event = published(at=time.time() - 29 * DAY)
        self.store.enqueue(event)
        self.assertEqual(self.poller.drain(), 1)
        self.store._exec("UPDATE kpi_applied SET at = ? WHERE event_id = ?", event.at, event.id)
        return event

    def total(self, event: DomainEvent) -> float:
        return sum(p.value for p in daily_rows(self.store, event.at))

    def test_a_replayed_event_inside_the_outbox_retention_is_still_not_counted_twice(self):
        policy = RetentionPolicy(outbox_days=30, kpi_hourly_days=31)      # the tightest gap the settings allow
        event = self.folded_29_days_ago()
        self.store.purge_expired(policy)
        self.assertEqual([e.id for e in self.store.poll("audit")], [event.id], "still inside the outbox retention")
        self.assertEqual(applied(self.store), {event.id}, "and its kpi_applied row outlives it")
        self.store.advance("insights", 0)                                  # the consumer's cursor is lost: a full replay
        self.assertEqual(self.poller.drain(), 1)
        self.assertEqual(self.total(event), 1.0, "counted once")
        self.assertEqual(len(hourly_rows(self.store, event.at)), 1)
        self.assertIn('inkcast_kpi_events_total{result="duplicate"} 1', self.metrics.render())

    def test_what_the_refused_order_would_do(self):
        """A policy the configuration refuses, applied straight to the store: the store still keeps ``kpi_applied``
        while the outbox holds the event (tests/test_review_retention.py), so even then the replay counts once."""
        policy = RetentionPolicy(outbox_days=30, kpi_hourly_days=20)
        self.assertTrue(retention_problems(policy), "never reaches the store through load_config or validate()")
        event = self.folded_29_days_ago()
        self.store.purge_expired(policy)
        self.assertEqual([e.id for e in self.store.poll("audit")], [event.id], "the event is still there to replay")
        self.assertEqual(applied(self.store), {event.id}, "and so is the record that makes a replay harmless")
        self.store.advance("insights", 0)
        self.assertEqual(self.poller.drain(), 1)
        self.assertEqual(self.total(event), 1.0, "counted once")


# ---- the housekeeping threads the configuration through ----------------------------------------------------


class SchedulerHousekeeping(unittest.TestCase):
    def context(self, policy: RetentionPolicy):
        cfg = by_hand(retention=policy, scheduler_interval_seconds=0.001)
        ctx = build(cfg, resolve("SchedulerWorker"))
        self.addCleanup(ctx.store.close)
        return ctx

    def seed(self, store: Store) -> tuple[DomainEvent, DomainEvent]:
        """An event 3 days old and one from today, both handled by every consumer."""
        seed_people(store, "u1")
        old, young = published(at=time.time() - 3 * DAY), published(at=time.time())
        store.enqueue(old, young)
        store.advance("insights", 2)
        return old, young

    def test_housekeeping_purges_with_the_configured_retention_not_the_default(self):
        ctx = self.context(RetentionPolicy(outbox_days=2, kpi_hourly_days=31))
        old, young = self.seed(ctx.store)
        housekeeping(ctx)
        self.assertEqual([e.id for e in ctx.store.poll("audit")], [young.id], "2 days configured: the 3-day-old event is gone")
        ctx = self.context(TODAY)
        old, young = self.seed(ctx.store)
        housekeeping(ctx)
        self.assertEqual([e.id for e in ctx.store.poll("audit")], [old.id, young.id], "7 days by default: both stay")

    def test_run_scheduler_hands_the_publisher_that_housekeeping(self):
        ctx = self.context(RetentionPolicy(outbox_days=2, kpi_hourly_days=31))
        old, young = self.seed(ctx.store)
        captured = {}

        class FakePublisher:
            async def run_forever(self, interval, stop, housekeeping=None, on_tick=None):
                captured.update(interval=interval, housekeeping=housekeeping, on_tick=on_tick)

        ctx.publisher = FakePublisher()
        asyncio.run(run_scheduler(ctx, asyncio.Event()))
        self.assertEqual(captured["interval"], 0.001)
        captured["housekeeping"]()
        self.assertEqual([e.id for e in ctx.store.poll("audit")], [young.id])
        captured["on_tick"]()                                              # the heartbeat still beats

    def test_the_real_loop_runs_it_every_90_ticks(self):
        ctx = self.context(RetentionPolicy(outbox_days=2, kpi_hourly_days=31))
        old, young = self.seed(ctx.store)

        async def run():
            stop = asyncio.Event()
            task = asyncio.create_task(run_scheduler(ctx, stop))
            deadline = time.monotonic() + 30
            while [e.id for e in ctx.store.poll("audit")] != [young.id] and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            stop.set()
            await asyncio.wait_for(task, 10)

        asyncio.run(run())
        self.assertEqual([e.id for e in ctx.store.poll("audit")], [young.id], "purged by the scheduler's own housekeeping")


# ---- the hosted environments --------------------------------------------------------------------------------


class HostedEnvironments(unittest.TestCase):
    """Production states both retentions explicitly, so the replay window and the hourly history are choices on
    record; staging rehearses production with the same values."""

    def literals(self, environment: str) -> dict[str, str]:
        k = yaml.safe_load((ROOT / "deploy" / "kustomize" / "environments" / environment / "kustomization.yaml").read_text())
        pairs = [lit.split("=", 1) for g in k.get("configMapGenerator", []) if g["name"] == "inkcast-config"
                 for lit in g.get("literals", [])]
        return {name: value for name, value in pairs}

    def test_production_and_staging_set_both_retentions_to_the_same_values_and_they_load(self):
        production, staging = self.literals("production"), self.literals("staging")
        for name in (OUTBOX, HOURLY):
            self.assertIn(name, production, f"production leaves {name} to the default")
            self.assertIn(name, staging, f"staging leaves {name} to the default")
            self.assertEqual(production[name], staging[name], name)
        cfg = config(**{name: production[name] for name in (OUTBOX, HOURLY)})
        cfg.validate()
        self.assertGreaterEqual(cfg.retention.kpi_hourly_days, cfg.retention.outbox_days)
        self.assertGreaterEqual(cfg.retention.kpi_hourly_days, MAX_HOURLY_DAYS)


if __name__ == "__main__":
    unittest.main()
