# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #157 (from the review of #155): the remaining bounds on the settings of F1.10.S5.

* ``INKCAST_RATE_LIMIT_PER_MINUTE`` has a ceiling, 10000: the limiter keeps the bucket as a float, and a value no float
  can hold (310 digits) made every rate-limited request, sign-in included, a 500. Above 600 with ``INKCAST_LOGIN=password``
  (ten guesses a second from one address) the limit no longer slows a guessing tool: a posture warning.
* ``INKCAST_KEYCLOAK_SYNC_INTERVAL`` and ``INKCAST_INSIGHTS_INTERVAL`` have a 1 s floor, like the scheduler's: 0.0001
  was accepted and is the tight loop 0 is refused for.
* ``INKCAST_OVERDUE_GRACE`` has a ceiling, 3600: beyond an hour the critical ``InkcastPostsOverdue`` alert, the operator's
  only signal that the scheduler is behind or stopped, stays quiet that long.
* ``INKCAST_SHUTDOWN_GRACE`` stays under the Deployment's ``terminationGracePeriodSeconds`` (40): at that point
  Kubernetes kills the pod, grace or no grace. The manifests and the ceiling are one number.
* ``INKCAST_KEYCLOAK_RECONCILE_INTERVAL`` keeps a 120 s margin under the alert window: the next reconcile is due that
  long after the last one *ends*, so its own run widens the gap, and Prometheus sees a counter move only at the next
  scrape (30 s) and judges it at the next evaluation (up to a minute).
* Staging and production state the eight settings explicitly (today's defaults), per the house rule.
* ``inkcast backup --keep`` above 35 warns as the posture check does for the environment variable."""

import io
import os
import pathlib
import re
import unittest
from contextlib import redirect_stderr
from unittest import mock

import yaml

from inkcast.libs.config import (
    INTERVAL_SETTINGS, KEYCLOAK_RECONCILE_CEILING, KEYCLOAK_RECONCILE_MARGIN, OVERDUE_GRACE_CEILING, RATE_LIMIT_CEILING,
    SHUTDOWN_GRACE_CEILING, SYNC_INTERVAL_CEILING, Config, ConfigError, load_config,
)
from inkcast.libs.http.ratelimit import RateLimiter
from inkcast.libs.secrets import Redactor, run_checks
from inkcast.libs.secrets.checks import BACKUP_KEEP_WARN, RATE_LIMIT_WARN

ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET_KEY = "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE"
BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": SECRET_KEY,
        "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
SYNC, RECONCILE, SCHEDULER, GRACE, INSIGHTS, SHUTDOWN = INTERVAL_SETTINGS
RATE_LIMIT, BACKUP_KEEP = "INKCAST_RATE_LIMIT_PER_MINUTE", "INKCAST_BACKUP_KEEP"
POLICY = {SYNC: "30", RECONCILE: "600", SCHEDULER: "20", GRACE: "600", INSIGHTS: "10", SHUTDOWN: "25",
          RATE_LIMIT: "60", BACKUP_KEEP: "14"}
KEYCLOAK = {"login": "oidc", "oidc_issuer": "https://sso.example.com/realms/inkcast", "oidc_client_id": "inkcast",
            "oidc_client_secret": "kc-secret", "oidc_required_role": "inkcast-user",
            "keycloak_sync_client_id": "inkcast-sync", "keycloak_sync_secret": "sync-secret"}
HUGE = "1" + "0" * 309                      # 310 digits: more than any float holds


def config(**env) -> Config:
    return load_config({**BASE, **env})


def by_hand(**fields) -> Config:
    return Config(**{"public_url": "https://inkcast.example.com", "secret_key": SECRET_KEY, "database_path": ":memory:",
                     "login": "password", "password": "p" * 12, **fields})


def checks(cfg: Config) -> dict:
    return {c.id: c for c in run_checks(cfg, redactor=Redactor(), environ={})}


def literals(path: str) -> dict[str, str]:
    """The inkcast-config literals of a kustomization."""
    doc = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    return dict(item.split("=", 1) for gen in doc.get("configMapGenerator", []) if gen["name"] == "inkcast-config"
                for item in gen.get("literals", []))


class Bounds(unittest.TestCase):
    def refused(self, name: str, *values: str, **env: str) -> None:
        for value in values:
            with self.subTest(**{name: value}):
                with self.assertRaises(ConfigError) as caught:
                    config(**{**env, name: value})
                message = str(caught.exception)
                self.assertIn(name, message)
                self.assertIn(repr(value), message, "the refusal quotes what was typed")
                self.assertEqual(message.count("\n- "), 0, f"one problem, stated plainly: {message}")

    def accepted(self, name: str, *values: str, **env: str) -> None:
        for value in values:
            with self.subTest(**{name: value}):
                config(**{**env, name: value}).validate()

    def test_a_rate_limit_no_float_can_hold_is_refused_not_a_500(self):
        """Before: accepted at load, and RateLimiter.allow raised OverflowError on every rate-limited request."""
        self.refused(RATE_LIMIT, HUGE, "10001", "100000")
        self.accepted(RATE_LIMIT, "0", "60", "600", "9999", "10000")
        self.assertEqual(RATE_LIMIT_CEILING, 10000)
        for value in (10**309, 10**400, RATE_LIMIT_CEILING + 1, RATE_LIMIT_CEILING):
            with self.subTest(value=value):
                try:
                    by_hand(rate_limit_per_minute=value).validate()
                except ConfigError as exc:
                    self.assertIn(RATE_LIMIT, str(exc))
                    continue
                # Accepted: then the limiter must serve it (the 500 was an OverflowError in here).
                self.assertEqual(RateLimiter(value).allow("login", "198.51.100.7"), 0.0)

    def test_the_rate_limit_rule_states_the_ceiling(self):
        with self.assertRaises(ConfigError) as caught:
            config(**{RATE_LIMIT: HUGE})
        self.assertIn("10000", str(caught.exception))
        self.assertNotIn(HUGE[:40], str(caught.exception).split("got")[0], "the ceiling, not the value, is the rule")

    def test_the_sync_and_insights_intervals_have_a_one_second_floor_like_the_scheduler(self):
        self.refused(SYNC, "0.0001", "0.5", "0.999")
        self.accepted(SYNC, "1", "1.5", "2", "30")
        self.refused(INSIGHTS, "0.0001", "0.001", "0.5", "0.999")
        self.accepted(INSIGHTS, "1", "1.5", "5", "10", "3600")
        for name, field in ((SYNC, "keycloak_sync_interval_seconds"), (INSIGHTS, "insights_interval_seconds")):
            for value in (0.5, 0.0001):
                with self.subTest(**{name: value}):
                    with self.assertRaises(ConfigError) as caught:
                        by_hand(**{field: value}).validate()
                    self.assertIn(name, str(caught.exception))
                    self.assertIn(" 1 ", str(caught.exception), "the floor is stated")

    def test_the_overdue_grace_has_a_ceiling_of_an_hour(self):
        self.assertEqual(OVERDUE_GRACE_CEILING, 3600)
        self.refused(GRACE, "3600.1", "3601", "7200", "86400")
        self.accepted(GRACE, "40", "600", "3599", "3600")
        with self.assertRaises(ConfigError) as caught:
            by_hand(overdue_grace_seconds=86400.0).validate()
        self.assertIn(GRACE, str(caught.exception))
        self.assertIn("3600", str(caught.exception))
        self.assertIn("InkcastPostsOverdue", str(caught.exception))

    def test_the_shutdown_grace_stays_under_the_termination_grace_period(self):
        self.assertEqual(SHUTDOWN_GRACE_CEILING, 40)
        self.refused(SHUTDOWN, "40", "40.0", "40.5", "41", "3600")
        self.accepted(SHUTDOWN, "25", "39", "39.9")
        with self.assertRaises(ConfigError) as caught:
            by_hand(shutdown_grace_seconds=40.0).validate()
        self.assertIn(SHUTDOWN, str(caught.exception))
        self.assertIn("terminationGracePeriodSeconds", str(caught.exception))
        self.assertIn("40", str(caught.exception))

    def test_the_manifests_termination_grace_period_is_the_ceiling(self):
        """The Deployment (and each split-services Deployment) gives the pod exactly the ceiling: a change to either
        moves both, and the setting can never reach the point where Kubernetes kills the pod."""
        found = []
        for path in ("deploy/kustomize/base/deployment.yaml", "deploy/kustomize/components/split-services/deployments.yaml"):
            for doc in yaml.safe_load_all((ROOT / path).read_text(encoding="utf-8")):
                if doc and doc.get("kind") == "Deployment":
                    found.append((path, doc["metadata"]["name"], doc["spec"]["template"]["spec"]["terminationGracePeriodSeconds"]))
        self.assertGreaterEqual(len(found), 2)
        for path, name, seconds in found:
            with self.subTest(path=path, deployment=name):
                self.assertEqual(seconds, SHUTDOWN_GRACE_CEILING)

    def test_the_reconcile_keeps_a_margin_under_the_alert_window(self):
        """The next reconcile is due KEYCLOAK_RECONCILE_INTERVAL after the last one *ends*, so its own run widens the gap
        between two counts of a failing person; Prometheus sees the counter move at the next scrape and judges it at the
        next evaluation. The margin covers a scrape (the ServiceMonitor's interval), Prometheus's default evaluation
        interval (a minute) and some seconds of run."""
        monitor = yaml.safe_load((ROOT / "deploy/kustomize/components/servicemonitor/servicemonitor.yaml").read_text("utf-8"))
        (scrape,) = {endpoint["interval"] for endpoint in monitor["spec"]["endpoints"]}
        self.assertRegex(scrape, r"^\d+s$")
        self.assertGreaterEqual(KEYCLOAK_RECONCILE_MARGIN, int(scrape[:-1]) + 60)
        self.assertEqual(KEYCLOAK_RECONCILE_MARGIN, 120)
        limit = KEYCLOAK_RECONCILE_CEILING - KEYCLOAK_RECONCILE_MARGIN                # 1680, with the sync interval
        self.assertEqual(SYNC_INTERVAL_CEILING, limit // 2)
        self.refused(RECONCILE, "1650.1", "1651", "1680", "1799")                      # the default sync interval is 30
        self.accepted(RECONCILE, "1649", "1650")
        self.refused(RECONCILE, "1081", **{SYNC: "600"})
        self.accepted(RECONCILE, "1080", **{SYNC: "600"})
        self.refused(SYNC, "840.5", "841", "899", "900")                              # no reconcile could follow
        self.accepted(SYNC, "840", **{RECONCILE: "840"})
        with self.assertRaises(ConfigError) as caught:
            config(**{RECONCILE: "1651"})
        self.assertIn("1650", str(caught.exception))
        self.assertIn("120", str(caught.exception))

    def test_every_lab_value_still_loads(self):
        """The walkthrough (sync 2, insights 1), the dev compose file and kind overlay (5), the ci and ci-oidc overlays."""
        walk = (ROOT / "scripts/dev/walkthrough/walk.py").read_text(encoding="utf-8")
        env = dict(re.findall(r'"(INKCAST_[A-Z_]+(?:INTERVAL|GRACE))": "([^"]*)"', walk))
        self.assertEqual((env[SYNC], env[INSIGHTS]), ("2", "1"))
        config(**env).validate()
        compose = yaml.safe_load((ROOT / "deploy/dev/compose.yaml").read_text(encoding="utf-8"))
        env = {k: str(v) for service in compose["services"].values() for k, v in (service.get("environment") or {}).items()
               if k in INTERVAL_SETTINGS}
        self.assertEqual(env[INSIGHTS], "5")
        config(**env).validate()
        for path in ("deploy/dev/kustomization.yaml", "deploy/kustomize/overlays/ci/kustomization.yaml",
                     "deploy/kustomize/overlays/ci-oidc/kustomization.yaml"):
            with self.subTest(path=path):
                env = {k: v for k, v in literals(path).items() if k in (*INTERVAL_SETTINGS, RATE_LIMIT, BACKUP_KEEP)}
                config(**env).validate()


class Posture(unittest.TestCase):
    def test_a_high_rate_limit_with_password_sign_in_warns(self):
        self.assertEqual(RATE_LIMIT_WARN, 600)
        for per_minute in (601, 1000, 10000):
            for environment in ("production", "staging", "development"):
                with self.subTest(per_minute=per_minute, environment=environment):
                    check = checks(by_hand(environment=environment, rate_limit_per_minute=per_minute))["rate-limit"]
                    self.assertEqual(check.status, "warn", check.detail)
                    self.assertIn(f"{RATE_LIMIT}={per_minute}", check.detail)
                    self.assertIn("INKCAST_LOGIN=password", check.detail)
                    self.assertIn("guess", check.detail)

    def test_a_high_rate_limit_with_another_sign_in_passes_and_one_at_the_line_passes(self):
        for per_minute in (601, 10000):
            check = checks(by_hand(environment="production", rate_limit_per_minute=per_minute, **KEYCLOAK))["rate-limit"]
            self.assertEqual(check.status, "pass", check.detail)
        for per_minute in (1, 60, 600):
            check = checks(by_hand(environment="production", rate_limit_per_minute=per_minute))["rate-limit"]
            self.assertEqual(check.status, "pass", check.detail)

    def test_the_launcher_logs_the_high_rate_limit_once(self):
        from inkcast.launcher import gate, resolve
        cfg = by_hand(environment="production", log_format="json", authz_allow_local=True, rate_limit_per_minute=601)
        with self.assertLogs("inkcast", level="WARNING") as logs:
            gate(cfg, resolve("IdentityService"))                            # a warning, not a refusal
        warned = [r for r in logs.records if getattr(r, "check", "") == "rate-limit"]
        self.assertEqual(len(warned), 1)


class Environments(unittest.TestCase):
    def test_staging_and_production_state_the_policy_settings_explicitly(self):
        for environment in ("staging", "production"):
            with self.subTest(environment=environment):
                found = literals(f"deploy/kustomize/environments/{environment}/kustomization.yaml")
                for name, default in POLICY.items():
                    self.assertEqual(found.get(name), default, f"{name} in {environment}")
                config(**{k: v for k, v in found.items() if k in POLICY}).validate()


class BackupCommand(unittest.TestCase):
    def run_backup(self, argv: list[str], **env: str) -> tuple[int, list]:
        from inkcast import backup
        environ = {**{k: v for k, v in os.environ.items() if k != BACKUP_KEEP}, **env}

        def fake_take_backup(database, directory, *, keep, version):
            return mock.Mock(name="backup", manifest={"size": 1, "sha256": "0" * 64, "rows": {"users": 0}})

        with mock.patch.dict("os.environ", environ, clear=True), mock.patch.object(backup, "take_backup", fake_take_backup), \
                mock.patch.object(backup, "_logging", lambda: None), redirect_stderr(io.StringIO()), \
                self.assertLogs("inkcast.backup", level="INFO") as logs:
            status = backup.backup_main(argv)
        return status, [r for r in logs.records if r.levelname == "WARNING"]

    def test_keep_above_35_warns_like_the_environment_variable(self):
        self.assertEqual(BACKUP_KEEP_WARN, 35)
        for argv, env in ((["--keep", "36"], {}), (["--keep", "400"], {}), ([], {BACKUP_KEEP: "36"})):
            with self.subTest(argv=argv, env=env):
                status, warned = self.run_backup(argv, **env)
                self.assertEqual(status, 0, "a warning, the backup still runs")
                self.assertEqual(len(warned), 1)
                self.assertEqual((warned[0].event, warned[0].check), ("config.posture.warn", "backup-keep"))
                self.assertIn("35", warned[0].getMessage())
                self.assertIn("--keep" if argv else BACKUP_KEEP, warned[0].getMessage())

    def test_keep_at_35_or_below_is_quiet(self):
        for argv, env in ((["--keep", "35"], {}), ([], {BACKUP_KEEP: "35"}), ([], {})):
            with self.subTest(argv=argv, env=env):
                self.assertEqual(self.run_backup(argv, **env), (0, []))


class Documentation(unittest.TestCase):
    def rows(self) -> dict[str, str]:
        return {line.split("|")[1].strip(): line for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
                if line.startswith("| `INKCAST_")}

    def test_the_readme_states_each_bound(self):
        rows = self.rows()
        self.assertIn("`10000`", rows[f"`{RATE_LIMIT}`"])
        self.assertIn("`600`", rows[f"`{RATE_LIMIT}`"])
        self.assertIn("`3600`", rows[f"`{GRACE}`"])
        self.assertIn("`40`", rows[f"`{SHUTDOWN}`"])
        self.assertIn("terminationGracePeriodSeconds", rows[f"`{SHUTDOWN}`"])
        self.assertNotIn("is longer", rows[f"`{SHUTDOWN}`"])
        self.assertIn("`1` to `840`", rows[f"`{SYNC}`"])
        self.assertIn("`1` or more", rows[f"`{INSIGHTS}`"])
        self.assertIn("`1680`", rows[f"`{RECONCILE}`"])
        self.assertIn("120", rows[f"`{RECONCILE}`"])

    def test_env_example_states_each_bound(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for said in ("0-10000", "above 600", "3600", "under 40", "terminationGracePeriodSeconds", "1-840", "1680"):
            with self.subTest(said=said):
                self.assertIn(said, text)

    def test_the_posture_table_states_the_high_rate_limit(self):
        text = (ROOT / "docs/reference/secrets.md").read_text(encoding="utf-8")
        (row,) = [line for line in text.splitlines() if line.startswith("| `rate-limit`")]
        self.assertIn("600", row)


if __name__ == "__main__":
    unittest.main()
