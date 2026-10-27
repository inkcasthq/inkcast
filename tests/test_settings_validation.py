# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""F1.10.S5 (issue #93): the settings that existed before the parameterisation stories took unsafe values silently.

Each is validated at start-up, at load (``load_config``) and in ``validate()`` for a Config built by hand, and every
refusal names the variable:

* ``INKCAST_KEYCLOAK_SYNC_INTERVAL``: a positive, finite number of seconds (0, a negative, nan or inf is a tight loop
  against Keycloak's admin API); 60 or more is a posture warning, since it breaks the "cut off within a minute" promise.
* ``INKCAST_KEYCLOAK_RECONCILE_INTERVAL``: from the sync interval (it is the catch-up for events the pass missed) to
  under 1800. The ``InkcastIdentitySyncPersonFailing`` alert looks at a 30-minute window, so a person who fails only in
  the reconcile could never raise it once the interval is 30 minutes or more: a hard bound, not a posture warning, since
  a warning logged once at start-up is missed for the lifetime of a deployment whose alert stays blind.
* ``INKCAST_SCHEDULER_INTERVAL``: 1 to 60 (120 s or more fails the on-time KPI and SLO by design).
* ``INKCAST_OVERDUE_GRACE``: at least twice the scheduler interval, or the critical ``InkcastPostsOverdue`` alert fires
  in normal operation.
* ``INKCAST_RATE_LIMIT_PER_MINUTE``: 0 (off) or more; 0 with ``INKCAST_LOGIN=password`` fails the production posture.
* ``INKCAST_BACKUP_KEEP``: a whole number, 1 or more, through one rule for the server and ``inkcast backup`` (which
  read it with a raw ``int()`` that could traceback); above 35 is a posture warning.
* ``INKCAST_INSIGHTS_INTERVAL`` and ``INKCAST_SHUTDOWN_GRACE``: positive and finite.

The defaults (30, 600, 20, 600, 10, 25, 60, 14) are what they were before and load clean."""

import io
import os
import pathlib
import re
import unittest
from contextlib import redirect_stderr
from unittest import mock

import yaml

from inkcast.libs.alerting import ALERTS
from inkcast.libs.config import (
    INTERVAL_SETTINGS, KEYCLOAK_RECONCILE_CEILING, OVERDUE_GRACE_TICKS, SCHEDULER_INTERVAL, Config,
    ConfigError, backup_keep_from, backup_keep_problems, interval_problems, load_config, rate_limit_problems,
)
from inkcast.libs.db.backup import DEFAULT_KEEP
from inkcast.libs.secrets import Redactor, run_checks
from inkcast.libs.secrets.checks import BACKUP_KEEP_WARN, SYNC_INTERVAL_WARN_SECONDS

ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET_KEY = "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE"
BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": SECRET_KEY,
        "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
SYNC, RECONCILE, SCHEDULER, GRACE, INSIGHTS, SHUTDOWN = INTERVAL_SETTINGS
RATE_LIMIT, BACKUP_KEEP = "INKCAST_RATE_LIMIT_PER_MINUTE", "INKCAST_BACKUP_KEEP"
DEFAULTS = {SYNC: 30.0, RECONCILE: 600.0, SCHEDULER: 20.0, GRACE: 600.0, INSIGHTS: 10.0, SHUTDOWN: 25.0}
FIELDS = {SYNC: "keycloak_sync_interval_seconds", RECONCILE: "keycloak_reconcile_seconds",
          SCHEDULER: "scheduler_interval_seconds", GRACE: "overdue_grace_seconds",
          INSIGHTS: "insights_interval_seconds", SHUTDOWN: "shutdown_grace_seconds",
          RATE_LIMIT: "rate_limit_per_minute", BACKUP_KEEP: "backup_keep"}
NOT_A_WAIT = ("0", "-1", "nan", "inf", "-inf", "abc", "")   # "" reads as unset, so it is the default: never refused
KEYCLOAK = {"login": "oidc", "oidc_issuer": "https://sso.example.com/realms/inkcast", "oidc_client_id": "inkcast",
            "oidc_client_secret": "kc-secret", "oidc_required_role": "inkcast-user",
            "keycloak_sync_client_id": "inkcast-sync", "keycloak_sync_secret": "sync-secret"}


def config(**env) -> Config:
    return load_config({**BASE, **env})


def by_hand(**fields) -> Config:
    return Config(**{"public_url": "https://inkcast.example.com", "secret_key": SECRET_KEY, "database_path": ":memory:",
                     "login": "password", "password": "p" * 12, **fields})


def checks(cfg: Config) -> dict:
    return {c.id: c for c in run_checks(cfg, redactor=Redactor(), environ={})}


class Refusals(unittest.TestCase):
    """Each refusal names the variable and quotes what was typed, at load and in validate() for a Config built by hand."""

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
                cfg = config(**{**env, name: value})
                cfg.validate()
                self.assertEqual(getattr(cfg, FIELDS[name]), float(value) if name in DEFAULTS else int(value))

    def test_the_sync_interval_is_a_positive_finite_number_of_seconds(self):
        self.refused(SYNC, "0", "-1", "-0.5", "nan", "inf", "-inf", "abc", "1e400")   # 1e400 reads as inf
        self.accepted(SYNC, "0.5", "1", "30", "59", "60", "600")                       # 60 and more: a posture warning

    def test_the_reconcile_interval_runs_from_the_sync_interval_to_under_30_minutes(self):
        self.refused(RECONCILE, "29.9", "29", "0", "-600", "nan", "inf", "abc")       # the default sync interval is 30
        self.refused(RECONCILE, "1770", "1799", "1799.9", "1800", "1800.5", "3600", "86400", "86401")   # 1800 with the sync
        self.refused(RECONCILE, "4.9", "1795", **{SYNC: "5"})
        self.accepted(RECONCILE, "30", "31", "600", "1769", "1769.9")
        self.accepted(RECONCILE, "5", "6", **{SYNC: "5"})
        self.accepted(RECONCILE, "600", **{SYNC: "600"})                              # equal is allowed

    def test_the_reconcile_rule_names_the_sync_interval_and_the_alert(self):
        with self.assertRaises(ConfigError) as caught:
            config(**{SYNC: "5", RECONCILE: "4"})
        message = str(caught.exception)
        self.assertIn(f"{SYNC} (5)", message)
        self.assertIn("1800", message)
        self.assertIn("InkcastIdentitySyncPersonFailing", message)
        self.assertIn("30-minute window", message)

    def test_a_reconcile_judged_against_a_sync_interval_outside_its_rule_is_judged_on_its_own(self):
        """One problem when only the sync interval is wrong; both when both are."""
        with self.assertRaises(ConfigError) as caught:
            config(**{SYNC: "abc", RECONCILE: "600"})
        self.assertIn(SYNC, str(caught.exception))
        self.assertNotIn(RECONCILE, str(caught.exception))
        with self.assertRaises(ConfigError) as caught:
            config(**{SYNC: "0", RECONCILE: "1800"})
        self.assertIn(SYNC, str(caught.exception))
        self.assertIn(RECONCILE, str(caught.exception))

    def test_the_scheduler_interval_is_1_to_60_seconds(self):
        self.refused(SCHEDULER, "0", "-5", "0.99", "60.01", "61", "120", "nan", "inf", "abc")
        self.accepted(SCHEDULER, "1", "1.5", "5", "20", "60")

    def test_the_overdue_grace_is_at_least_two_scheduler_intervals(self):
        self.refused(GRACE, "39", "39.9", "0", "-600", "nan", "inf", "abc")           # the default tick is 20
        self.accepted(GRACE, "40", "40.1", "600", "86400")
        self.refused(GRACE, "9", "9.5", **{SCHEDULER: "5"})
        self.accepted(GRACE, "10", "10.5", **{SCHEDULER: "5"})
        self.refused(GRACE, "119", **{SCHEDULER: "60"})
        self.accepted(GRACE, "120", **{SCHEDULER: "60"})

    def test_the_overdue_grace_rule_names_the_tick_and_the_alert(self):
        with self.assertRaises(ConfigError) as caught:
            config(**{SCHEDULER: "5", GRACE: "9"})
        message = str(caught.exception)
        self.assertIn(f"{SCHEDULER} (10)", message, "twice the tick, as the operator would write it")
        self.assertIn("InkcastPostsOverdue", message)

    def test_a_grace_judged_against_a_tick_outside_its_rule_is_judged_against_two_seconds(self):
        with self.assertRaises(ConfigError) as caught:
            config(**{SCHEDULER: "0", GRACE: "600"})
        self.assertIn(SCHEDULER, str(caught.exception))
        self.assertNotIn(GRACE, str(caught.exception))
        with self.assertRaises(ConfigError) as caught:
            config(**{SCHEDULER: "abc", GRACE: "1"})
        self.assertIn(SCHEDULER, str(caught.exception))
        self.assertIn(f"{SCHEDULER} (2)", str(caught.exception))

    def test_the_insights_interval_and_the_shutdown_grace_are_positive_and_finite(self):
        for name in (INSIGHTS, SHUTDOWN):
            self.refused(name, "0", "-1", "nan", "inf", "-inf", "abc")
            self.accepted(name, "0.001", "1", "10", "25", "3600")

    def test_the_rate_limit_is_a_whole_number_0_or_more(self):
        self.refused(RATE_LIMIT, "-1", "-60", "1.5", "nan", "abc")
        self.accepted(RATE_LIMIT, "0", "1", "60", "10000")                              # 0 is off: a posture check

    def test_backup_keep_is_a_whole_number_1_or_more(self):
        self.refused(BACKUP_KEEP, "0", "-1", "two", "1.5", "nan")
        self.accepted(BACKUP_KEEP, "1", "14", "35", "36", "400")                         # above 35: a posture warning

    def test_an_empty_value_is_the_default_not_a_refusal(self):
        cfg = config(**{name: "" for name in (*INTERVAL_SETTINGS, RATE_LIMIT, BACKUP_KEEP)})
        cfg.validate()
        self.assertEqual({name: getattr(cfg, FIELDS[name]) for name in DEFAULTS}, DEFAULTS)
        self.assertEqual((cfg.rate_limit_per_minute, cfg.backup_keep), (60, 14))

    def test_several_wrong_at_once_are_listed_together_each_naming_its_variable(self):
        with self.assertRaises(ConfigError) as caught:
            config(**{SYNC: "0", SCHEDULER: "0", INSIGHTS: "nan", SHUTDOWN: "-1"})
        message = str(caught.exception)
        self.assertTrue(message.startswith("Inkcast configuration problems:\n- "))
        for name in (SYNC, SCHEDULER, INSIGHTS, SHUTDOWN):
            self.assertIn(name, message)
        self.assertEqual(message.count("\n- "), 4, message)

    def test_a_config_built_by_hand_is_refused_by_validate_naming_the_variable(self):
        cases = {SYNC: (0, -1.0, float("nan"), float("inf"), "30", True),
                 RECONCILE: (29.0, 0, KEYCLOAK_RECONCILE_CEILING, 1800.5, float("nan"), "600"),
                 SCHEDULER: (0, 0.5, 60.5, 120, float("inf"), "20"),
                 GRACE: (39.0, 0, -1, float("nan"), "600"),
                 INSIGHTS: (0, -10.0, float("nan"), "10"),
                 SHUTDOWN: (0, -25.0, float("inf"), "25"),
                 RATE_LIMIT: (-1, 1.5, "60", True, None),
                 BACKUP_KEEP: (0, -1, 1.5, "14", True, None)}
        for name, values in cases.items():
            for value in values:
                with self.subTest(**{name: value}):
                    with self.assertRaises(ConfigError) as caught:
                        by_hand(**{FIELDS[name]: value}).validate()
                    self.assertIn(name, str(caught.exception))
        by_hand().validate()                                                  # the defaults, as a Config built by hand
        by_hand(**{FIELDS[SCHEDULER]: 5, FIELDS[GRACE]: 10, FIELDS[SYNC]: 5, FIELDS[RECONCILE]: 5,
                   FIELDS[RATE_LIMIT]: 0, FIELDS[BACKUP_KEEP]: 1}).validate()  # every boundary at once

    def test_the_rules_are_stated_as_problems_naming_the_variable(self):
        """The rule functions, as the newer settings have them: what load_config and validate() both apply."""
        self.assertEqual(interval_problems(sync=30.0, reconcile=600.0, scheduler=20.0, grace=600.0, insights=10.0,
                                           shutdown=25.0), [])
        problems = interval_problems(sync=0, reconcile=1800, scheduler=0, grace=0, insights=0, shutdown=0)
        self.assertEqual([p.split(" ")[0] for p in problems], list(INTERVAL_SETTINGS))
        self.assertEqual(rate_limit_problems(0), [])
        self.assertEqual([p.split(" ")[0] for p in rate_limit_problems(-1, got="-1")], [RATE_LIMIT])
        self.assertIn("'-1'", rate_limit_problems(-1, got="-1")[0])
        self.assertEqual(backup_keep_problems(1), [])
        self.assertEqual([p.split(" ")[0] for p in backup_keep_problems(0, got="0")], [BACKUP_KEEP])
        self.assertIn("'0'", backup_keep_problems(0, got="0")[0])


class Defaults(unittest.TestCase):
    def test_the_defaults_are_what_they_were_and_load_clean(self):
        cfg = config()
        cfg.validate()
        self.assertEqual({name: getattr(cfg, FIELDS[name]) for name in DEFAULTS}, DEFAULTS)
        self.assertEqual((cfg.rate_limit_per_minute, cfg.backup_keep), (60, DEFAULT_KEEP))
        self.assertEqual(SCHEDULER_INTERVAL, (1, 60))
        self.assertEqual(OVERDUE_GRACE_TICKS, 2)
        self.assertEqual(BACKUP_KEEP_WARN, 35)
        self.assertEqual(SYNC_INTERVAL_WARN_SECONDS, 60)
        for check in ("keycloak-sync-interval", "rate-limit", "backup-keep"):
            self.assertIn(checks(cfg)[check].status, ("pass", "info"), check)

    def test_the_reconcile_ceiling_is_the_alert_window(self):
        """The bound exists for the InkcastIdentitySyncPersonFailing alert's range: a change to either moves both."""
        (alert,) = [a for a in ALERTS if a.name == "InkcastIdentitySyncPersonFailing"]
        (window,) = re.findall(r"\[(\d+)m\]", alert.expr)
        self.assertEqual(KEYCLOAK_RECONCILE_CEILING, int(window) * 60)
        self.assertLess(DEFAULTS[RECONCILE], KEYCLOAK_RECONCILE_CEILING)

    def test_the_lab_values_load(self):
        """The dev compose file, the CI OIDC overlay and the walkthrough set short intervals: all within the rules."""
        compose = yaml.safe_load((ROOT / "deploy/dev/compose.yaml").read_text(encoding="utf-8"))
        env = {k: str(v) for service in compose["services"].values() for k, v in (service.get("environment") or {}).items()
               if k in INTERVAL_SETTINGS}
        self.assertIn(SCHEDULER, env)
        config(**env).validate()
        overlay = yaml.safe_load((ROOT / "deploy/kustomize/overlays/ci-oidc/kustomization.yaml").read_text(encoding="utf-8"))
        literals = dict(item.split("=", 1) for gen in overlay.get("configMapGenerator", []) for item in gen.get("literals", []))
        env = {k: v for k, v in literals.items() if k in INTERVAL_SETTINGS}
        self.assertIn(SYNC, env)
        config(**env).validate()
        walk = (ROOT / "scripts/dev/walkthrough/walk.py").read_text(encoding="utf-8")
        env = {name: value for name, value in re.findall(r'"(INKCAST_[A-Z_]+(?:INTERVAL|GRACE))": "([^"]*)"', walk)}
        self.assertEqual(set(env), {SYNC, SCHEDULER, INSIGHTS})
        config(**env).validate()


class Posture(unittest.TestCase):
    def test_a_sync_interval_of_a_minute_or_more_warns_everywhere(self):
        for seconds, shown in ((60, "60"), (60.0, "60"), (61.5, "61.5"), (3600, "3600")):
            for environment in ("production", "staging", "ci", "development"):
                for fields in (KEYCLOAK, {}):
                    with self.subTest(seconds=seconds, environment=environment, sync="on" if fields else "off"):
                        cfg = by_hand(environment=environment, keycloak_sync_interval_seconds=seconds, **fields)
                        check = checks(cfg)["keycloak-sync-interval"]
                        self.assertEqual(check.status, "warn", check.detail)
                        self.assertIn(f"{SYNC}={shown}", check.detail)
                        self.assertIn("minute", check.detail)

    def test_a_sync_interval_under_a_minute_passes_with_the_sync_in_use_and_is_noted_without(self):
        for seconds in (59.9, 30, 1, 0.5):
            with self.subTest(seconds=seconds):
                check = checks(by_hand(environment="production", keycloak_sync_interval_seconds=seconds, **KEYCLOAK))
                self.assertEqual(check["keycloak-sync-interval"].status, "pass", check["keycloak-sync-interval"].detail)
                check = checks(by_hand(environment="production", keycloak_sync_interval_seconds=seconds))
                self.assertEqual(check["keycloak-sync-interval"].status, "info")
                self.assertIn("not in use", check["keycloak-sync-interval"].detail)

    def test_the_rate_limit_off_with_password_sign_in_fails_production_and_warns_elsewhere(self):
        for environment, status in (("production", "fail"), ("staging", "warn"), ("ci", "warn"), ("development", "warn")):
            with self.subTest(environment=environment):
                check = checks(by_hand(environment=environment, rate_limit_per_minute=0))["rate-limit"]
                self.assertEqual(check.status, status, check.detail)
                self.assertIn(f"{RATE_LIMIT}=0", check.detail)
                self.assertIn("INKCAST_LOGIN=password", check.detail)

    def test_the_rate_limit_off_with_another_sign_in_is_a_choice_on_record(self):
        for environment in ("production", "development"):
            with self.subTest(environment=environment):
                check = checks(by_hand(environment=environment, rate_limit_per_minute=0, **KEYCLOAK))["rate-limit"]
                self.assertEqual(check.status, "info", check.detail)
                self.assertIn(f"{RATE_LIMIT}=0", check.detail)

    def test_a_rate_limit_passes(self):
        for per_minute in (1, 60, 10000):
            for fields in ({}, KEYCLOAK):
                check = checks(by_hand(environment="production", rate_limit_per_minute=per_minute, **fields))["rate-limit"]
                self.assertEqual(check.status, "pass", check.detail)
                self.assertIn(f"{RATE_LIMIT}={per_minute}", check.detail)

    def test_backups_kept_beyond_35_days_warn_everywhere(self):
        for keep, status in ((1, "pass"), (14, "pass"), (35, "pass"), (36, "warn"), (400, "warn")):
            for environment in ("production", "development"):
                with self.subTest(keep=keep, environment=environment):
                    check = checks(by_hand(environment=environment, backup_keep=keep))["backup-keep"]
                    self.assertEqual(check.status, status, check.detail)
                    self.assertIn(f"{BACKUP_KEEP}={keep}", check.detail)
        self.assertIn("35", checks(by_hand(backup_keep=36))["backup-keep"].detail)

    def test_the_launcher_refuses_production_with_the_rate_limit_off_and_password_sign_in(self):
        from inkcast.launcher import gate, resolve
        cfg = by_hand(environment="production", log_format="json", authz_allow_local=True, rate_limit_per_minute=0)
        with self.assertRaises(ConfigError) as caught:
            gate(cfg, resolve("IdentityService"))
        self.assertIn(RATE_LIMIT, str(caught.exception))
        cfg = by_hand(environment="production", log_format="json", authz_allow_local=True, rate_limit_per_minute=1)
        gate(cfg, resolve("IdentityService"))                                 # the same with any limit starts

    def test_the_launcher_logs_the_warnings_once(self):
        from inkcast.launcher import gate, resolve
        cfg = by_hand(environment="staging", log_format="json", authz_allow_local=True, keycloak_sync_interval_seconds=60,
                      backup_keep=36, **KEYCLOAK)
        with self.assertLogs("inkcast", level="WARNING") as logs:
            gate(cfg, resolve("IdentityService"))
        for check in ("keycloak-sync-interval", "backup-keep"):
            warned = [r for r in logs.records if getattr(r, "check", "") == check]
            self.assertEqual(len(warned), 1, check)
            self.assertEqual(warned[0].event, "config.posture.warn")

    def test_the_table_has_the_checks_and_none_crashes(self):
        table = checks(by_hand())
        for check in ("keycloak-sync-interval", "rate-limit", "backup-keep"):
            self.assertIn(check, table)
        self.assertNotIn("check crashed", " ".join(c.detail for c in table.values()))


class BackupCommand(unittest.TestCase):
    """``inkcast backup`` reads INKCAST_BACKUP_KEEP through the server's rule: a bad value is a usage error (exit 2)
    naming the variable, never a traceback; ``--keep`` is held to the same rule."""

    def run_backup(self, argv: list[str], **env: str) -> tuple[int, str, int | None]:
        from inkcast import backup
        seen: list[int] = []

        def fake_take_backup(database, directory, *, keep, version):
            seen.append(keep)
            return mock.Mock(name="backup", manifest={"size": 1, "sha256": "0" * 64, "rows": {"users": 0}})

        stderr = io.StringIO()
        environ = {**{k: v for k, v in os.environ.items() if k != BACKUP_KEEP}, **env}   # unset unless the test sets it
        with mock.patch.dict("os.environ", environ, clear=True), mock.patch.object(backup, "take_backup", fake_take_backup), \
                mock.patch.object(backup, "_logging", lambda: None), redirect_stderr(stderr):
            try:
                status = backup.backup_main(argv)
            except SystemExit as exc:                                   # argparse's usage error
                status = exc.code
        return status, stderr.getvalue(), seen[0] if seen else None

    def test_a_bad_variable_is_a_usage_error_naming_it_not_a_traceback(self):
        for value in ("two", "0", "-1", "1.5"):
            with self.subTest(value=value):
                status, err, keep = self.run_backup([], INKCAST_BACKUP_KEEP=value)
                self.assertEqual((status, keep), (2, None))
                self.assertIn(BACKUP_KEEP, err)
                self.assertIn(repr(value), err)
                self.assertNotIn("Traceback", err)

    def test_a_bad_keep_option_is_a_usage_error_under_the_same_rule(self):
        for value in ("two", "0", "-1"):
            with self.subTest(value=value):
                status, err, keep = self.run_backup(["--keep", value])
                self.assertEqual((status, keep), (2, None))
                self.assertIn("--keep", err)
                self.assertIn(repr(value), err)
                self.assertNotIn("Traceback", err)

    def test_the_variable_the_option_and_the_default_reach_the_backup(self):
        self.assertEqual(self.run_backup([], INKCAST_BACKUP_KEEP="3")[::2], (0, 3))
        self.assertEqual(self.run_backup(["--keep", "5"], INKCAST_BACKUP_KEEP="3")[::2], (0, 5))   # the option wins
        self.assertEqual(self.run_backup([])[::2], (0, DEFAULT_KEEP))
        self.assertEqual(backup_keep_from({}), DEFAULT_KEEP)
        self.assertEqual(backup_keep_from({}), Config(public_url="http://localhost:8787", secret_key=SECRET_KEY,
                                                      database_path=":memory:").backup_keep)
        with self.assertRaisesRegex(ConfigError, BACKUP_KEEP):
            backup_keep_from({"INKCAST_BACKUP_KEEP": "two"})


class Documentation(unittest.TestCase):
    def test_the_readme_table_states_each_default_and_bound(self):
        rows = {line.split("|")[1].strip(): line for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
                if line.startswith("| `INKCAST_")}
        for name, default in ((SYNC, "30"), (RECONCILE, "600"), (SCHEDULER, "20"), (GRACE, "600"), (INSIGHTS, "10"),
                              (SHUTDOWN, "25"), (RATE_LIMIT, "60"), (BACKUP_KEEP, "14")):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", rows, f"no README row for {name}")
                self.assertTrue(rows[f"`{name}`"].startswith(f"| `{name}` | `{default}` |"), rows[f"`{name}`"])
        self.assertIn("`1` to `60`", rows[f"`{SCHEDULER}`"])
        self.assertIn("twice `INKCAST_SCHEDULER_INTERVAL`", rows[f"`{GRACE}`"])
        self.assertIn("`1800`", rows[f"`{RECONCILE}`"])
        self.assertIn("InkcastIdentitySyncPersonFailing", rows[f"`{RECONCILE}`"])
        self.assertIn("`60` or more is a posture warning", rows[f"`{SYNC}`"])
        self.assertIn("password", rows[f"`{RATE_LIMIT}`"])
        self.assertIn("`35`", rows[f"`{BACKUP_KEEP}`"])

    def test_env_example_lists_each_with_its_default(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for name, default in ((SYNC, "30"), (RECONCILE, "600"), (SCHEDULER, "20"), (GRACE, "600"), (INSIGHTS, "10"),
                              (SHUTDOWN, "25"), (RATE_LIMIT, "60"), (BACKUP_KEEP, "14")):
            with self.subTest(name=name):
                self.assertIn(f"# {name}={default}\n", text)

    def test_the_posture_table_has_the_three_checks(self):
        text = (ROOT / "docs/reference/secrets.md").read_text(encoding="utf-8")
        for check, said in (("keycloak-sync-interval", "60"), ("rate-limit", "password"), ("backup-keep", "35")):
            with self.subTest(check=check):
                (row,) = [line for line in text.splitlines() if line.startswith(f"| `{check}`")]
                self.assertIn(said, row)


if __name__ == "__main__":
    unittest.main()
