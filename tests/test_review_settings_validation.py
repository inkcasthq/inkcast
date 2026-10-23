# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #155 (issue #93): the existing business settings validated at start-up. Each test failed
on the PR's head."""

import asyncio
import re
import unittest

import httpx2

from inkcast.libs.alerting import ALERTS
from inkcast.libs.config import ConfigError, load_config
from inkcast.libs.db import Store

ISSUER = "https://sso.example.com/realms/inkcast"
SECRET_KEY = "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE"
KEYCLOAK = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": SECRET_KEY,
            "INKCAST_LOGIN": "oidc", "OIDC_ISSUER": ISSUER, "OIDC_CLIENT_ID": "inkcast", "OIDC_CLIENT_SECRET": "kc-secret",
            "OIDC_REQUIRED_ROLE": "inkcast-user", "INKCAST_KEYCLOAK_SYNC_CLIENT_ID": "inkcast-sync",
            "INKCAST_KEYCLOAK_SYNC_SECRET": "sync-secret"}
SYNC, RECONCILE = "INKCAST_KEYCLOAK_SYNC_INTERVAL", "INKCAST_KEYCLOAK_RECONCILE_INTERVAL"
T0 = 1_700_000_000.0


def alert_window() -> int:
    """The InkcastIdentitySyncPersonFailing alert's range, in seconds, read from its expression."""
    (alert,) = [a for a in ALERTS if a.name == "InkcastIdentitySyncPersonFailing"]
    (window,) = re.findall(r"\[(\d+)m\]", alert.expr)
    return int(window) * 60


class Failures:
    """The metrics the sync counts into, recording when each person failure was counted (the fake clock's time)."""

    def __init__(self, now: dict[str, float]):
        self.now, self.at = now, []

    def inc(self, name: str, **labels) -> None:
        if name == "inkcast_identity_sync_errors_total":
            self.at.append(self.now["t"])

    def __getattr__(self, name):                 # any other metric call is irrelevant here
        return lambda *args, **kwargs: None


def person_failure_gaps(env: dict[str, str], reconciles: int = 3) -> list[float]:
    """The seconds between the person failures counted for one person Keycloak answers 500 about, who is read only by
    the reconcile, under ``env`` as load_config reads it: the passes run every sync interval, exactly (no time spent in
    a pass, the most favourable case for the bound)."""
    from inkcast.services.identity.keycloak_sync import KeycloakSync
    cfg = load_config({**KEYCLOAK, **env})
    store = Store(":memory:", "k" * 40)
    try:
        store.upsert_user(f"oidc:{ISSUER}#kc-ok", "ok", None, None)
        store.upsert_user(f"oidc:{ISSUER}#kc-failing", "failing", None, None)

        def keycloak(request: httpx2.Request) -> httpx2.Response:
            path = request.url.path
            if path.endswith("/token"):
                return httpx2.Response(200, json={"access_token": "t", "expires_in": 3_000_000})
            if path.endswith(("/admin-events", "/events")):
                return httpx2.Response(200, json=[])
            if path.endswith("/kc-failing"):
                return httpx2.Response(500, json={"error": "unknown_error"})
            return httpx2.Response(200, json={"enabled": True})

        now = {"t": T0}
        failures = Failures(now)

        async def run() -> None:
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(keycloak)) as http:
                worker = KeycloakSync(cfg, store, http, metrics=failures, clock=lambda: now["t"])
                passes = 0
                while len(failures.at) <= reconciles and passes < 100_000:
                    await worker.tick()
                    passes += 1
                    now["t"] += cfg.keycloak_sync_interval_seconds

        asyncio.run(run())
    finally:
        store.close()
    return [b - a for a, b in zip(failures.at, failures.at[1:])]


class TheReconcileFailsAPersonAgainWithinTheAlertWindow(unittest.TestCase):
    """The PR bounds INKCAST_KEYCLOAK_RECONCILE_INTERVAL below 1800 so that a person who fails only in the reconcile is
    counted again within the InkcastIdentitySyncPersonFailing alert's 30-minute window. But the reconcile is not a timer
    of its own: it runs at the first events pass after it is due (``tick``), so up to one sync interval late. With the
    default sync interval (30) and a reconcile of 1799, which the PR accepts, the person is counted every 1800 s; with a
    sync interval of 1000 and a reconcile of 1700, every 2000 s. The alert's expression is then false between two
    failures, its one-hour hold never completes, and it never fires: the case the bound exists for."""

    def test_every_accepted_pair_counts_a_person_failing_in_the_reconcile_again_within_the_window(self):
        window = alert_window()
        ran = 0
        for sync, reconcile in (("30", "600"), ("30", "1799"), ("5", "1799"), ("1000", "1700"), ("600", "1080")):
            env = {SYNC: sync, RECONCILE: reconcile}
            with self.subTest(**env):
                try:
                    load_config({**KEYCLOAK, **env})
                except ConfigError:
                    continue                                 # refused at start-up: it never runs
                ran += 1
                with self.assertLogs("inkcast.identity.sync", "WARNING"):         # each failure is logged: kept quiet
                    gaps = person_failure_gaps(env)
                self.assertTrue(gaps, "the failing person is counted at each reconcile")
                self.assertLess(max(gaps), window,
                                f"accepted with {SYNC}={sync}, {RECONCILE}={reconcile}, the person is counted every "
                                f"{max(gaps):g} s: the alert's {window} s window has a gap, so its hold never completes")
        self.assertGreaterEqual(ran, 2, "the default and a pair inside the bound run")


class ASyncIntervalNoReconcileCanFollowIsRefusedAsItself(unittest.TestCase):
    """The README and .env.example give INKCAST_KEYCLOAK_SYNC_INTERVAL no upper bound ("more than 0; 60 or more is a
    posture warning"), and its rule has none. But the reconcile may be no shorter than it and must stay under the
    alert's window with it, so from some sync interval on no reconcile can follow: the server refuses to start, and the
    refusal names INKCAST_KEYCLOAK_RECONCILE_INTERVAL, which the operator never set and cannot set to anything that
    starts. The PR's own rule is that a setting judged against another outside its rule is judged on its own, so one
    wrong value gives one problem about itself."""

    def test_the_refusal_names_the_sync_interval_and_every_sync_interval_it_accepts_has_a_reconcile(self):
        for sync in ("900", "1800", "3600", "86400"):
            with self.subTest(sync=sync):
                with self.assertRaises(ConfigError) as caught:
                    load_config({**KEYCLOAK, SYNC: sync})              # the reconcile left at its default, 600
                message = str(caught.exception)
                self.assertTrue(message.startswith(f"{SYNC} must"), message)
                self.assertNotIn("\n- ", message, "one problem: the default reconcile is not one")
                self.assertIn(repr(sync), message)
        for sync in ("1", "30", "60", "600", "839", "840"):                           # 840 at most, with #157's margin
            with self.subTest(sync=sync):
                load_config({**KEYCLOAK, SYNC: sync, RECONCILE: sync}).validate()   # the shortest reconcile it allows

    def test_the_docs_state_the_sync_intervals_bound(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        (row,) = [line for line in (root / "README.md").read_text(encoding="utf-8").splitlines()
                  if line.startswith(f"| `{SYNC}` |")]
        self.assertIn("`1` to `840`", row)
        self.assertIn("1-840", (root / ".env.example").read_text(encoding="utf-8"))


class AShutdownGraceUnderASecondIsStillAGrace(unittest.TestCase):
    """INKCAST_SHUTDOWN_GRACE is now refused at 0 or below, the hole being no grace at all for in-flight requests. But
    the launcher hands it to uvicorn as ``int(grace)``, and uvicorn waits ``asyncio.wait_for(..., timeout=0)`` for the
    requests in flight, which cancels them at once: ``0.5`` (or the ``0.001`` the PR's own test accepts) is exactly the
    ``0`` it refuses, and ``1.5`` loses half its grace."""

    def test_the_grace_load_config_accepts_reaches_uvicorn_whole(self):
        from inkcast.launcher import _Server

        async def app(scope, receive, send):                  # never called: the server is only configured
            raise AssertionError("not served")

        ran = 0
        for grace in ("0.5", "0.001", "1.5", "25"):
            with self.subTest(grace=grace):
                try:
                    cfg = load_config({**KEYCLOAK, "INKCAST_SHUTDOWN_GRACE": grace})
                except ConfigError:
                    continue                                   # refused at start-up: never a server
                ran += 1
                server = _Server(app, "127.0.0.1", 0, cfg.shutdown_grace_seconds, "127.0.0.1")
                timeout = server.server.config.timeout_graceful_shutdown
                self.assertGreater(timeout, 0, f"INKCAST_SHUTDOWN_GRACE={grace} gives in-flight requests no time at all")
                self.assertGreaterEqual(timeout, cfg.shutdown_grace_seconds, f"INKCAST_SHUTDOWN_GRACE={grace}")
        self.assertGreaterEqual(ran, 2, "1.5 and the default start")


class AnIntervalBeyondAnyFloatIsRefusedNotACrash(unittest.TestCase):
    """``validate()`` is the PR's net for a Config built by hand, each refusal a ConfigError naming the variable. An int
    too large for a float (``10**400``) makes ``math.isfinite`` raise OverflowError instead: a traceback, no variable
    named, and none of the other problems listed."""

    def test_validate_refuses_it_naming_the_variable(self):
        from inkcast.libs.config import Config
        for field, name in (("keycloak_sync_interval_seconds", SYNC), ("keycloak_reconcile_seconds", RECONCILE),
                            ("scheduler_interval_seconds", "INKCAST_SCHEDULER_INTERVAL"),
                            ("overdue_grace_seconds", "INKCAST_OVERDUE_GRACE"),
                            ("insights_interval_seconds", "INKCAST_INSIGHTS_INTERVAL"),
                            ("shutdown_grace_seconds", "INKCAST_SHUTDOWN_GRACE")):
            with self.subTest(field=field):
                cfg = Config(public_url="https://inkcast.example.com", secret_key=SECRET_KEY, database_path=":memory:",
                             login="password", password="p" * 12, **{field: 10**400})
                try:
                    cfg.validate()
                except ConfigError as exc:
                    self.assertIn(name, str(exc))
                except OverflowError as exc:
                    self.fail(f"{field}=10**400: {type(exc).__name__}: {exc}, not a refusal naming {name}")
                else:
                    self.fail(f"{field}=10**400 passed validate()")


if __name__ == "__main__":
    unittest.main()
