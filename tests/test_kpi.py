"""libs/kpi: the catalogue has a producer for every built KPI, rollups land in hourly and daily buckets per
scope, replays are no-ops, and ratios come from their parts."""

import time
import unittest

from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.events import EVENT_TYPES, DomainEvent
from inkcast.libs.kpi import BY_NAME, CATALOGUE, ON_TIME_SECONDS, RULES, KpiStore, RollupEngine, bucket_start
from inkcast.libs.observability import Metrics
from inkcast.libs.runtime import build_metrics


class Catalogue(unittest.TestCase):
    def test_every_built_kpi_has_a_producer_and_every_planned_one_an_epic(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        metrics = build_metrics(store, {}, Config(public_url="http://localhost:8787", secret_key="k" * 40,
                                                  database_path=":memory:"))
        registered = set(metrics._help)
        rules_by_kpi = {r.kpi for r in RULES}
        for kpi in CATALOGUE:
            with self.subTest(kpi=kpi.name):
                if kpi.status == "planned":
                    self.assertTrue(kpi.epic and kpi.producer, "a planned KPI names its epic and its future producer")
                    continue
                if kpi.source == "prometheus":
                    self.assertIn(kpi.producer, registered, "a Prometheus-sourced KPI names a registered metric")
                elif kpi.source == "events" and kpi.aggregation == "ratio":
                    self.assertIn(kpi.numerator, rules_by_kpi)
                    for part in kpi.denominator:
                        self.assertIn(part, rules_by_kpi)
                elif kpi.source == "events":
                    self.assertIn(kpi.producer, EVENT_TYPES, "an event-sourced KPI names a catalogued event")
                    self.assertIn(kpi.name, rules_by_kpi, "and a rollup rule produces it")
                elif kpi.source == "db":
                    self.assertTrue(callable(getattr(KpiStore, kpi.producer, None)), "a DB KPI names a KpiStore reader")
        self.assertEqual({r.kpi for r in RULES} - set(BY_NAME), set(), "every rule feeds a catalogued KPI")

    def test_customer_numbers_never_become_prometheus_labels(self):
        for kpi in CATALOGUE:
            if kpi.layer in ("customer", "team"):
                self.assertNotEqual(kpi.source, "prometheus", kpi.name)
                self.assertIn(kpi.visibility, ("can_view_usage", "can_view_audit", "self"), kpi.name)
        self.assertEqual(len({k.name for k in CATALOGUE}), len(CATALOGUE), "names are unique")


class Rollups(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.metrics = Metrics()
        self.metrics.counter("inkcast_kpi_events_total", "")
        self.engine = RollupEngine(self.store, metrics=self.metrics)
        self.t = 1_700_000_000.0  # 2023-11-14T22:13:20Z: hour 22, so the hour and day buckets differ

    def published(self, user="u1", platform="devto", lag=None, scheduled=False, workspace=None, at=None, **more):
        attrs = {"platform": platform, "status": "published", "scheduled": scheduled, "lag_seconds": lag, **more}
        return DomainEvent("post.published", user_id=user, workspace_id=workspace, subject="p", attrs=attrs, at=at or self.t)

    def test_events_land_in_hour_and_day_buckets_per_scope_and_replays_are_no_ops(self):
        events = [self.published(), self.published(platform="medium"), self.published(user="u2"),
                  DomainEvent("post.failed", user_id="u1", attrs={"platform": "devto", "reason": "platform"}, at=self.t),
                  DomainEvent("sync.result", user_id="u1", attrs={"platform": "devto", "action": "update"}, at=self.t)]
        self.engine.handle(events)
        self.engine.handle(events)                                          # the outbox replayed the batch
        hour, day = bucket_start(self.t, "hour"), bucket_start(self.t, "day")
        self.assertEqual((hour, day), (1_699_999_200, 1_699_920_000))
        u1 = self.store.read("posts_published", "hour", "user", "u1", hour, hour + 1)
        self.assertEqual([(p.dims["platform"], p.value, p.n) for p in u1], [("devto", 1.0, 1), ("medium", 1.0, 1)])
        self.assertEqual(sum(p.value for p in self.store.read("posts_published", "day", "user", "u1", day, day + 86400)), 2)
        self.assertEqual(sum(p.value for p in self.store.read("posts_published", "day", "user", "u2", day, day + 86400)), 1)
        self.assertEqual(self.store.read("posts_published", "day", "user", "u1", day, day + 86400, dims={"platform": "medium"})[0].n, 1)
        self.assertEqual(sum(p.value for p in self.store.read("posts_failed", "day", "user", "u1", day, day + 1)), 1)
        self.assertEqual(self.store.read("sync_results", "hour", "user", "u1", hour, hour + 1)[0].dims,
                         {"platform": "devto", "action": "update"})
        # application-wide rows per platform, so the owner's view needs no scan over customers
        self.assertEqual(sum(p.value for p in self.store.read("posts_published", "day", "platform", "devto", day, day + 1)), 2)
        self.assertEqual(self.store.distinct_scopes("posts_published", "user", day, day + 86400), 2)
        self.assertIn('inkcast_kpi_events_total{result="applied"} 5', self.metrics.render())
        self.assertIn('inkcast_kpi_events_total{result="duplicate"} 5', self.metrics.render())

    def test_on_time_late_lag_and_retries(self):
        self.engine.handle([self.published(scheduled=True, lag=30.0), self.published(scheduled=True, lag=ON_TIME_SECONDS + 1),
                            self.published(),                                               # a "now" post: not timed
                            DomainEvent("post.scheduled", user_id="u1", attrs={"platform": "devto"}, at=self.t),
                            DomainEvent("post.scheduled", user_id="u1", attrs={"platform": "devto", "retry": 2}, at=self.t)])
        day = bucket_start(self.t, "day")

        def total(kpi):
            return sum(p.value for p in self.store.read(kpi, "day", "user", "u1", day, day + 86400))
        self.assertEqual((total("posts_on_time"), total("posts_late")), (1, 1))
        self.assertEqual(total("schedule_lag_seconds"), 30.0 + ON_TIME_SECONDS + 1)
        self.assertEqual((total("posts_scheduled"), total("posts_retried")), (1, 1))

    def test_team_kpis_need_a_workspace_and_carry_the_member(self):
        self.engine.handle([self.published(user="u1"), self.published(user="u1", workspace="ws1"),
                            self.published(user="u2", workspace="ws1")])
        day = bucket_start(self.t, "day")
        self.assertEqual(self.store.read("member_posts_published", "day", "user", "u1", day, day + 1), [])   # never per user
        rows = self.store.read("member_posts_published", "day", "workspace", "ws1", day, day + 1)
        self.assertEqual(sorted((p.dims["member"], p.value) for p in rows), [("u1", 1.0), ("u2", 1.0)])
        self.assertEqual(sum(p.value for p in self.store.read("posts_published", "day", "workspace", "ws1", day, day + 1)), 2)

    def test_hourly_rows_expire_and_daily_rows_stay(self):
        self.engine.handle([self.published(at=time.time() - 100 * 86400), self.published(at=time.time())])
        self.assertEqual(self.store.purge_kpis(hourly_keep_seconds=90 * 86400), 2)   # user + platform rows of the old hour
        old_day = bucket_start(time.time() - 100 * 86400, "day")
        self.assertEqual(len(self.store.read("posts_published", "day", "user", "u1", old_day, old_day + 1)), 1)


if __name__ == "__main__":
    unittest.main()
