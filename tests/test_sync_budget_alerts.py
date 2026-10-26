# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #180: a pass that ends on the timeout budget, or never finishes, raises an alert.

#152 made a pass end early, as a success for what it did, once its budget of reads about people with no answer was
used. That was a log line only (``identity.sync.pass.ended``): a partial outage (one user-storage provider down
while local users answer) looked like success at every pass, ``InkcastIdentitySyncFailing`` never fired, and a pass
that never finishes counted nothing at all, so nothing saw a sync that was stuck. Now:

* ``inkcast_identity_sync_ended_early_total{stage}`` counts the passes that ended on the budget, by the stage they
  ended in (``events``, ``admin-events``, ``reconcile``); a pass that fails on the budget (nothing answered) is a
  failed pass, counted as before, not here. ``InkcastIdentitySyncReconcileEndingEarly`` fires when every 30 minutes
  of an hour has had a reconcile end on it.
* ``inkcast_identity_sync_reconcile_age_seconds`` is the time since the last full reconcile (one that tried everyone
  not known to hang), kept in the store (``sync_state``), so it survives a restart and a crash loop; from the
  process start until there has been one. ``inkcast_identity_sync_reconcile_interval_seconds`` is the setting, so
  ``InkcastIdentitySyncStale`` fires past three intervals without one: a sync that is stuck, or one that keeps
  finding new people who hang (reads that time out at random) and never reaches the rest.

Keycloak ids carry non-hex letters (``kc-ada``) so an Inkcast id (``usr_<hex>``) can't contain one by chance."""

import re
import unittest

import httpx2
from test_sync_per_person_errors import T0
from test_sync_timeout_budget import Budget

from inkcast.libs.alerting import ALERTS
from inkcast.libs.config import KEYCLOAK_RECONCILE_CEILING, Config
from inkcast.libs.db import Store
from inkcast.libs.runtime import build_metrics
from inkcast.libs.secrets import SecretValue
from inkcast.services.identity.keycloak_sync import KeycloakSync

ISSUER = "https://kc.example/realms/inkcast"
ENDED = "inkcast_identity_sync_ended_early_total"
AGE = "inkcast_identity_sync_reconcile_age_seconds"
INTERVAL = "inkcast_identity_sync_reconcile_interval_seconds"
BY_NAME = {a.name: a for a in ALERTS}


class Counted(Budget):
    def ended_early(self, stage: str) -> int:
        return self.counted.count((ENDED, (("stage", stage),)))


class APassThatEndsOnTheBudgetIsCounted(Counted):
    def test_once_in_the_reconcile(self):
        """Two people answer (one gone), then three hang: the reconcile ends on the budget, a success for what it
        did (``AnOutageThatBeginsMidPass``). On main that was a log line only."""
        people = self.people(*(f"kc-p{i}" for i in range(6)))
        self.kc.gone.add(people[0]["kc"])
        for user in people[2:5]:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        worker = self.sync(30, reconcile_seconds=600)
        self.assertIsInstance(self.reconcile(worker, T0), dict)
        self.assertEqual(self.ended_early("reconcile"), 1)
        self.assertEqual((self.ended_early("events"), self.ended_early("admin-events")), (0, 0))
        self.assertEqual(self.pass_failures_counted(), 0, "a success for what it did")
        self.assertIsInstance(self.tick(worker, T0 + 30), dict)     # due again: the sixth, then the known three
        self.assertEqual(self.ended_early("reconcile"), 2)
        self.assertIsInstance(self.tick(worker, T0 + 60), dict)     # the events path alone: no budget used
        self.assertEqual(self.ended_early("reconcile"), 2)

    def test_once_in_the_events_pass(self):
        """A person whose DELETE_ACCOUNT event is the newest answers (erased); the three next events are people who
        hang: the events pass ends on the budget, and the admin-events pass and the reconcile do not run."""
        people = self.people(*(f"kc-p{i}" for i in range(4)))
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["erased"], 0, "the cursors are set")
        for user in people[:3]:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
            self.kc.events.append(self.event(user["kc"], 1_000))
        self.kc.gone.add(people[3]["kc"])
        self.kc.events.append(self.event(people[3]["kc"], 2_000))   # the newest: read first, answers
        done = self.tick(worker, T0 + 3)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(done["erased"], 1)
        self.assertEqual(self.ended_early("events"), 1)
        self.assertEqual((self.ended_early("admin-events"), self.ended_early("reconcile")), (0, 0))

    def test_once_in_the_admin_events_pass(self):
        people = self.people(*(f"kc-p{i}" for i in range(4)))
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        for user in people[:3]:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
            self.admin_event(user["kc"], 1_000)
        self.kc.disabled.add(people[3]["kc"])
        self.admin_event(people[3]["kc"], 2_000)                   # the newest: read first, answers
        done = self.tick(worker, T0 + 3)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(done["disabled"], 1)
        self.assertEqual(self.ended_early("admin-events"), 1)
        self.assertEqual((self.ended_early("events"), self.ended_early("reconcile")), (0, 0))

    def test_a_pass_that_fails_on_the_budget_is_a_failed_pass_not_one_that_ended_early(self):
        """Nothing answered: the pass fails as a whole (``InkcastIdentitySyncFailing``'s count), and is not counted
        here too."""
        people = self.people(*(f"kc-p{i}" for i in range(5)))
        for user in people:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        worker = self.sync(30, reconcile_seconds=600)
        self.assertIsNone(self.reconcile(worker, T0))
        self.assertEqual(self.ended_early("reconcile"), 0)
        self.assertEqual(self.ended_early("events"), 0)

    def test_a_pass_within_its_budget_is_not_counted(self):
        hangs, gone = self.people("kc-ada", "kc-bob")
        self.kc.no_answer[hangs["kc"]] = httpx2.ReadTimeout
        self.kc.gone.add(gone["kc"])
        worker = self.sync(30)
        self.assertIsInstance(self.reconcile(worker, T0), dict)
        self.assertEqual(sum(self.ended_early(s) for s in ("events", "admin-events", "reconcile")), 0)


class Gauges(Budget):
    """The real registry (``build_metrics``), on the scenario's clock, so the gauges can be read back."""

    def config(self, days: "int | None" = 30, reconcile_seconds: float = 600.0, sync: bool = True) -> Config:
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="oidc",
                     oidc_issuer=ISSUER, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                     oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync" if sync else "",
                     keycloak_sync_secret=SecretValue("sync-secret") if sync else SecretValue(""),
                     keycloak_erase_after_days=days, keycloak_reconcile_seconds=reconcile_seconds)
        cfg.validate()
        return cfg

    def sync(self, days: "int | None" = 30, reconcile_seconds: float = 600.0) -> KeycloakSync:
        cfg = self.config(days, reconcile_seconds)
        self.metrics = build_metrics(self.store, {}, cfg, clock=lambda: self.now)
        worker = KeycloakSync(cfg, self.store, self.http, self.metrics, clock=lambda: self.now)
        worker.last_reconcile = self.now                            # a pass is the events path alone until reconcile()
        return worker

    def sample(self, name: str, metrics=None) -> "float | None":
        lines = [ln for ln in (metrics or self.metrics).render().splitlines() if ln.startswith(name + " ")]
        self.assertLessEqual(len(lines), 1, f"{name} is rendered at most once, without labels")
        return float(lines[0].split()[1]) if lines else None


class TheReconcileAge(Gauges):
    def test_is_the_time_since_the_last_full_reconcile(self):
        gone, fine = self.people("kc-ada", "kc-bob")
        self.kc.gone.add(gone["kc"])
        worker = self.sync(30, reconcile_seconds=600)
        self.assertEqual(self.sample(AGE), 0.0, "from the process start until the first")
        self.now = T0 + 200
        self.assertEqual(self.sample(AGE), 200.0)
        self.assertIsInstance(self.reconcile(worker, T0 + 200), dict)
        self.assertEqual(self.sample(AGE), 0.0, "a reconcile that tried everyone")
        self.now = T0 + 500
        self.assertEqual(self.sample(AGE), 300.0)
        self.assertIsInstance(self.tick(worker, T0 + 500), dict)    # the events path alone: not a reconcile
        self.assertEqual(self.sample(AGE), 300.0)
        self.assertIsInstance(self.tick(worker, T0 + 800), dict)    # due: a reconcile
        self.assertEqual(self.sample(AGE), 0.0)
        self.assertEqual(self.sample(INTERVAL), 600.0, "the setting, for the alert's threshold")

    def test_a_reconcile_the_budget_ended_with_people_untried_does_not_count(self):
        """``AnOutageThatBeginsMidPass``: the sixth person was not tried, so the reconcile was not of everyone; the
        next pass tries them, and is."""
        people = self.people(*(f"kc-p{i}" for i in range(6)))
        self.kc.gone.add(people[0]["kc"])
        for user in people[2:5]:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        worker = self.sync(30, reconcile_seconds=600)
        self.assertIsInstance(self.reconcile(worker, T0), dict)
        self.now = T0 + 30
        self.assertEqual(self.sample(AGE), 30.0, "not a full reconcile: the age runs on")
        self.assertIsInstance(self.tick(worker, T0 + 30), dict)
        self.assertEqual(self.sample(AGE), 0.0, "everyone not known to hang was tried")
        self.assertIsInstance(self.tick(worker, T0 + 630), dict)   # on schedule: the three known to hang, read last
        self.assertEqual(self.sample(AGE), 0.0, "they cost the budget, but nobody who could answer was left untried")

    def test_a_reconcile_that_could_read_nobody_does_not_count(self):
        people = self.people("kc-ada", "kc-bob", "kc-carol")
        for user in people:
            self.kc.failing[user["kc"]] = 403
        worker = self.sync(30, reconcile_seconds=600)
        self.now = T0 + 100
        self.assertIsNone(self.reconcile(worker, T0 + 100))
        self.assertEqual(self.sample(AGE), 100.0)

    def test_survives_a_restart_and_is_the_same_for_every_process(self):
        """The time is in the store, not the process: after a restart (a crash loop that never finishes a
        reconcile) the age runs from the last full reconcile, not from the restart; a second process reads the
        same value."""
        (fine,) = self.people("kc-ada")
        worker = self.sync(30, reconcile_seconds=600)
        self.assertIsInstance(self.reconcile(worker, T0), dict)
        self.now = T0 + 1000
        self.sync(30, reconcile_seconds=600)                        # a new worker and a new registry, the same store
        self.assertEqual(self.sample(AGE), 1000.0, "since the reconcile at T0, not since the restart")
        other = build_metrics(self.store, {}, self.config(), clock=lambda: self.now)   # another process, same store
        self.assertEqual(self.sample(AGE, other), 1000.0)
        self.assertIsNone(self.sample(ENDED), "a counter, with no series yet")

    def test_before_any_reconcile_it_runs_from_the_process_start(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        metrics = build_metrics(store, {}, self.config(), clock=lambda: self.now)
        self.now = T0 + 2000
        self.assertEqual(self.sample(AGE, metrics), 2000.0, "a sync that never finished its first reconcile")

    def test_neither_gauge_exists_where_the_sync_is_off(self):
        metrics = build_metrics(self.store, {}, self.config(sync=False), clock=lambda: self.now)
        self.now = T0 + 2000
        self.assertIsNone(self.sample(AGE, metrics), "no series: the alert cannot fire where there is no sync")
        self.assertIsNone(self.sample(INTERVAL, metrics))
        self.assertIn(f"# TYPE {AGE} gauge", metrics.render(), "registered all the same")
        self.assertIn(f"# TYPE {ENDED} counter", metrics.render())


class TheShapesTheIssueNames(Gauges):
    def ended_early(self, stage: str) -> int:
        return int(self.metrics.snapshot().get(ENDED, {}).get((("stage", stage),), 0))

    def stale(self) -> bool:
        """``InkcastIdentitySyncStale``'s condition, from the gauges as the alert reads them."""
        return self.sample(AGE) > 3 * self.sample(INTERVAL)

    def test_a_partial_outage_counts_at_every_reconcile_and_the_age_does_not_grow(self):
        """One user-storage provider is down for its five people while five local people answer: every pass is a
        success, so ``InkcastIdentitySyncFailing`` never fires, but every reconcile ends on the budget (after the
        five are known: everyone else read first, then three of them), and the counter says so. The age stays
        within an interval: nobody who could answer is left untried."""
        people = self.people(*(f"kc-p{i:02d}" for i in range(10)))
        for user in people[0:10:2]:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        worker = self.sync(30, reconcile_seconds=600)
        results = [self.reconcile(worker, T0), *self.passes(worker, T0 + 30, 60)]   # half an hour of passes
        self.assertTrue(all(isinstance(r, dict) for r in results), "every pass a success: the alert on failures is blind")
        self.assertEqual(self.pass_failures_counted(), 0)
        # At T0 (three found, five left untried: due again), at T0+30 (the other two found; everyone who could answer
        # was tried), then on schedule at T0+630 and T0+1230: each ended on the budget.
        self.assertEqual(self.ended_early("reconcile"), 4)
        self.assertEqual(worker.last_reconcile, T0 + 1230)
        self.assertEqual(self.sample(AGE), 570.0, "since the last reconcile, which tried everyone who could answer")
        self.assertFalse(self.stale())
        for user in people[1:10:2]:
            self.assert_untouched(user)

    def test_reads_that_time_out_at_random_never_reach_the_rest_and_the_age_says_so(self):
        """Each pass, three people not yet known to hang get no answer (a flaky provider): the budget ends every
        reconcile with people untried who are not known to hang, so none is full, and after three intervals the
        age is past the alert's line. Nothing else fires: every pass succeeds for what it did."""
        people = self.people(*(f"kc-p{i:02d}" for i in range(30)))
        worker = self.sync(30, reconcile_seconds=60)                # the shortest interval the tests' sync allows
        pool = [u["kc"] for u in people]
        for n in range(8):                                          # four minutes of passes
            fresh, pool = pool[:3], pool[3:]
            self.kc.no_answer = {k: httpx2.ReadTimeout for k in fresh}
            self.assertIsInstance(self.tick(worker, T0 + 30 * n) if n else self.reconcile(worker, T0), dict)
            self.assertEqual(worker.last_reconcile, 0, f"pass {n}: people not known to hang were left untried")
        self.assertEqual(self.ended_early("reconcile"), 8)
        self.assertEqual(self.pass_failures_counted(), 0)
        self.assertEqual(self.sample(AGE), 210.0, "since the process start: no full reconcile yet")
        self.assertTrue(self.stale(), "past three intervals (180 s) without one")
        self.kc.no_answer = {}                                      # the provider recovers
        self.assertIsInstance(self.tick(worker, T0 + 240), dict)
        self.assertEqual(self.sample(AGE), 0.0)
        self.assertFalse(self.stale())


class TheAlerts(unittest.TestCase):
    def test_reconciles_that_keep_ending_on_the_budget_warn_like_a_person_failing(self):
        alert = BY_NAME["InkcastIdentitySyncReconcileEndingEarly"]
        self.assertEqual(alert.severity, "warning")
        self.assertIn(f'{ENDED}{{stage="reconcile"}}', alert.expr)
        (window,) = re.findall(r"\[(\d+)m\]", alert.expr)
        self.assertEqual(int(window) * 60, KEYCLOAK_RECONCILE_CEILING, "a reconcile in every window, whatever the interval")
        self.assertEqual(alert.for_, "1h")
        self.assertEqual(alert.service, "identity")
        self.assertTrue(alert.runbook.endswith("#inkcastidentitysyncreconcileendingearly"))

    def test_a_stale_sync_warns_past_three_reconcile_intervals(self):
        alert = BY_NAME["InkcastIdentitySyncStale"]
        self.assertEqual(alert.severity, "warning")
        self.assertIn(AGE, alert.expr)
        self.assertIn(f"3 * max({INTERVAL})", alert.expr)
        self.assertEqual(alert.service, "identity")
        self.assertIn("INKCAST_KEYCLOAK_RECONCILE_INTERVAL", alert.description)
        self.assertTrue(alert.runbook.endswith("#inkcastidentitysyncstale"))

    def test_the_generated_rules_carry_both(self):
        import pathlib
        import yaml
        root = pathlib.Path(__file__).resolve().parents[1]
        for path in ("deploy/kustomize/components/prometheus-rules/prometheusrule.yaml", "deploy/dev/prometheus/rules.yaml"):
            with self.subTest(path=path):
                doc = yaml.safe_load((root / path).read_text(encoding="utf-8"))
                groups = doc["spec"]["groups"] if "spec" in doc else doc["groups"]
                names = {r["alert"] for g in groups for r in g["rules"] if "alert" in r}
                self.assertLessEqual({"InkcastIdentitySyncReconcileEndingEarly", "InkcastIdentitySyncStale"}, names)


if __name__ == "__main__":
    unittest.main()
