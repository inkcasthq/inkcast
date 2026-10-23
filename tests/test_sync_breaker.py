# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""E9.F9.5.S7 (issue #105): a circuit breaker for a mass "user not found".

Keycloak answers 404 ``User not found`` about people who still exist when it was restored from an older backup, when
the issuer points at the wrong Keycloak, or when a non-importing user-storage provider (LDAP) is disabled for
maintenance. Before this story the reconcile cut every one of them off at once and erased them when the grace period
ended (at the same reconcile with INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=0). Now the reconcile counts the people Keycloak
says are gone before acting on any of them: at INKCAST_KEYCLOAK_BREAKER_MIN (5) or more, and above
INKCAST_KEYCLOAK_BREAKER_PERCENT (10) of those it read, the breaker trips. Nobody is cut off or erased, no grace
period starts or advances, the pass is recorded with the people's ids, ``inkcast_identity_sync_breaker_open`` goes
to 1 and ``InkcastIdentitySyncBreakerOpen`` fires, until an operator confirms the pass (``inkcast identity
confirm-deletions --pass <id>``: exactly its people are cut off and their grace period starts) or dismisses it
(``dismiss-deletions``: nothing is done; the next reconcile reads everyone again). A self-deletion found by a
DELETE_ACCOUNT event is never held: the person asked, and the event is specific. The trip lives in the database, so
a restart changes nothing. Every setting is validated at start-up, and ``off`` is a posture warning in production
and staging.

Module-level imports name only what main has, so on main the threshold tests fail on their assertions (everyone is
cut off) rather than on an import."""

import asyncio
import dataclasses
import io
import logging
import os
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.config import Config, ConfigError, load_config
from inkcast.libs.db import Store
from inkcast.libs.runtime import build_metrics
from inkcast.libs.secrets import SecretValue
from inkcast.services.identity.keycloak_sync import USER_EVENTS_CURSOR, KeycloakSync

ISSUER = "https://kc.example/realms/inkcast"
PREFIX = f"oidc:{ISSUER}#"
DAY = 86400
T0 = 1_700_000_000.0
MODES = {"30 days": 30, "0 days": 0, "off": None}
KEY = "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE"


class Keycloak:
    """A Keycloak admin API over ``httpx2.MockTransport``: who is gone (404 ``User not found``), who is disabled, the
    USER admin events and the DELETE_ACCOUNT user events (filtered by type and user, newest first), and which people
    the sync read."""

    def __init__(self):
        self.gone: set[str] = set()
        self.disabled: set[str] = set()
        self.events: list[dict] = []
        self.admin_events: list[dict] = []
        self.read: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path, q = request.url.path, request.url.params
        if path.endswith("/token"):
            return httpx2.Response(200, json={"access_token": "t", "expires_in": 300})
        if path.endswith("/admin-events"):
            return httpx2.Response(200, json=sorted(self.admin_events, key=lambda e: -e["time"]))
        if path.endswith("/events"):
            found = [e for e in self.events if e["type"] in q.get_list("type") and q.get("user") in (None, e["userId"])]
            return httpx2.Response(200, json=sorted(found, key=lambda e: -e["time"])[: int(q.get("max", "100"))])
        keycloak_id = path.rsplit("/", 1)[1]
        self.read.append(keycloak_id)
        if keycloak_id in self.gone:
            return httpx2.Response(404, json={"error": "User not found"})
        return httpx2.Response(200, json={"id": keycloak_id, "enabled": keycloak_id not in self.disabled})


class Scenario(unittest.TestCase):
    """One database file (the operator's command opens it on its own), one mock Keycloak, a clock the test moves, and
    the real metrics registry so the gauge can be read as Prometheus would."""

    def setUp(self):
        self.kc = Keycloak()
        self.database = os.path.join(tempfile.mkdtemp(), "inkcast.db")
        self.store = Store(self.database, KEY)
        self.addCleanup(self.store.close)
        self.now = T0
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.kc))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))

    def people(self, n: int, prefix: str = "p") -> list[dict]:
        """``n`` Keycloak-backed people, each with a live access token; sorted as the reconcile walks them."""
        rows = []
        for i in range(n):
            name = f"{prefix}{i:03d}"
            user = self.store.upsert_user(PREFIX + name, name, None, None)
            self.store.save_token(f"ic_at_{name:x<43}", "access", "c1", user["id"], ["inkcast"], None, f"f-{name}", 10 * DAY)
            rows.append({**user, "kc": name})
        return sorted(rows, key=lambda u: u["id"])

    def config(self, days: "int | None" = 30, **breaker) -> Config:
        cfg = Config(public_url="http://localhost:8787", secret_key=KEY, database_path=self.database, login="oidc",
                     oidc_issuer=ISSUER, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                     oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync",
                     keycloak_sync_secret=SecretValue("sync-secret"), keycloak_erase_after_days=days, **breaker)
        cfg.validate()
        return cfg

    def sync(self, days: "int | None" = 30, **breaker) -> KeycloakSync:
        cfg = self.config(days, **breaker)
        self.metrics = build_metrics(self.store, {}, cfg)
        worker = KeycloakSync(cfg, self.store, self.http, self.metrics, clock=lambda: self.now)
        worker.last_reconcile = self.now                            # a pass is the events path alone until reconcile()
        return worker

    def tick(self, worker: KeycloakSync, at: "float | None" = None) -> dict:
        if at is not None:
            self.now = at
        return asyncio.run(worker.tick())

    def reconcile(self, worker: KeycloakSync, at: "float | None" = None) -> dict:
        worker.last_reconcile = 0
        return self.tick(worker, at)

    def row(self, user: dict) -> "dict | None":
        return self.store.get_user(user["id"])

    def gauge(self) -> int:
        line = [ln for ln in self.metrics.render().splitlines() if ln.startswith("inkcast_identity_sync_breaker_open ")]
        self.assertEqual(len(line), 1, "the gauge is rendered once, without labels")
        return int(float(line[0].split()[1]))

    def counted(self, name: str) -> dict:
        return {dict(labels).get("via") or dict(labels).get("reason"): int(v) for labels, v in self.metrics.snapshot().get(name, {}).items()}

    def trips(self) -> list[dict]:
        return self.store.breaker_trips()

    def assert_untouched(self, user: dict) -> None:
        row = self.row(user)
        self.assertIsNotNone(row, f"{user['kc']} keeps their row")
        self.assertIsNone(row["disabled_at"], f"{user['kc']} is not cut off")
        self.assertIsNone(row["deleted_at"], f"{user['kc']}: no grace period runs")
        self.assertIsNotNone(self.store.get_token(f"ic_at_{user['kc']:x<43}", "access"), f"{user['kc']}'s token lives")

    def assert_cut_off(self, user: dict, deleted_at: "float | None" = None) -> None:
        row = self.row(user)
        self.assertIsNotNone(row, f"{user['kc']} keeps their row")
        self.assertIsNotNone(row["disabled_at"], f"{user['kc']} is cut off")
        self.assertIsNone(self.store.get_token(f"ic_at_{user['kc']:x<43}", "access"), f"{user['kc']}'s token is dead")
        if deleted_at is not None:
            self.assertEqual(row["deleted_at"], deleted_at, f"{user['kc']}: the grace period runs from the right time")

    def assert_erased(self, user: dict) -> None:
        self.assertIsNone(self.row(user), f"{user['kc']} is erased")

    def operator(self, *argv: str) -> tuple[int, str]:
        """``inkcast identity ...`` against this database, with the test's clock; (exit status, stdout)."""
        from inkcast.breaker import identity_main
        out = io.StringIO()
        environ = {"INKCAST_DATABASE": self.database, "INKCAST_SECRET_KEY": KEY, "INKCAST_LOGIN": "password",
                   "INKCAST_PASSWORD": "p" * 12}
        return identity_main(list(argv), environ=environ, clock=lambda: self.now, out=out), out.getvalue()


class BelowTheThreshold(Scenario):
    def test_fewer_than_min_gone_are_cut_off_as_before_whatever_the_share(self):
        people = self.people(8)
        gone, kept = people[:4], people[4:]                         # 4 of 8 is 50 %, but under the minimum of 5
        self.kc.gone.update(u["kc"] for u in gone)
        worker = self.sync(30)
        done = self.reconcile(worker)
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (8, 4, 0))
        for user in gone:
            self.assert_cut_off(user, deleted_at=T0)
        for user in kept:
            self.assert_untouched(user)
        self.assertEqual(self.trips(), [], "no trip")
        self.assertEqual(self.gauge(), 0)
        self.assertEqual(self.reconcile(worker, T0 + 30 * DAY)["erased"], 4, "erased after the grace period, as before")

    def test_min_or_more_gone_but_not_above_the_percent_are_cut_off_as_before(self):
        people = self.people(50)
        gone = people[:5]                                           # 5 of 50: exactly 10 %, not above it
        self.kc.gone.update(u["kc"] for u in gone)
        worker = self.sync(0)
        done = self.reconcile(worker)
        self.assertEqual((done["disabled"], done["erased"]), (5, 5), "0 days: erased at this reconcile, as before")
        for user in gone:
            self.assert_erased(user)
        for user in people[5:]:
            self.assert_untouched(user)
        self.assertEqual(self.trips(), [])
        self.assertEqual(self.counted("inkcast_identity_users_deleted_total"), {"reconcile": 5})

    def test_a_slow_drip_never_trips(self):
        """The count is of people newly found gone in one pass: one already cut off, with a grace period running, is
        not news. Two gone each reconcile for a week never reaches the minimum, however many are gone in all."""
        people = self.people(20)
        worker = self.sync(30)
        for i in range(5):
            self.kc.gone.update(u["kc"] for u in people[2 * i: 2 * i + 2])
            done = self.reconcile(worker, T0 + i * DAY)
            self.assertEqual((done["disabled"], done["erased"]), (2, 0), f"reconcile {i}: the two newly gone are cut off")
        self.assertEqual(self.trips(), [], "10 of 20 gone over five days: never 5 at once")
        self.assertEqual(self.reconcile(worker, T0 + 30 * DAY)["erased"], 2, "the first pair, 30 days after its cut-off")


class AtAndAboveTheThreshold(Scenario):
    def test_the_breaker_trips_and_nobody_is_cut_off_or_erased_in_any_mode(self):
        for mode, days in MODES.items():
            with self.subTest(mode=mode):
                self.setUp()
                people = self.people(20)
                gone = people[:5]                                   # 5 of 20: at the minimum and 25 % > 10 %
                self.kc.gone.update(u["kc"] for u in gone)
                worker = self.sync(days)
                with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
                    done = self.reconcile(worker)
                self.assertEqual(done, {"checked": 20, "disabled": 0, "erased": 0})
                for user in people:
                    self.assert_untouched(user)
                self.assertEqual(self.store.user_erasures(), [], "no erasure, not even with 0 days")
                self.assertEqual(self.counted("inkcast_identity_users_disabled_total"), {})
                self.assertEqual(self.counted("inkcast_identity_users_deleted_total"), {})
                trips = self.trips()
                self.assertEqual(len(trips), 1, "one trip record")
                trip = trips[0]
                self.assertEqual((trip["status"], trip["gone"], trip["read"], trip["tripped_at"]), ("pending", 5, 20, T0))
                self.assertEqual(sorted(p["user_id"] for p in self.store.breaker_trip_people(trip["id"])),
                                 sorted(u["id"] for u in gone), "exactly the people found gone")
                self.assertEqual(self.gauge(), 1, "inkcast_identity_sync_breaker_open 1")
                tripped = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.tripped"]
                self.assertEqual(len(tripped), 1)
                self.assertEqual((tripped[0].pass_id, tripped[0].gone, tripped[0].read), (trip["id"], 5, 20))
                self.assertEqual(tripped[0].levelno, logging.CRITICAL)
                self.assertNotIn(gone[0]["kc"], tripped[0].getMessage(), "no Keycloak id in the log")
                self.assertNotIn(gone[0]["id"], tripped[0].getMessage(), "no user id in the log line either")
                self.assertEqual(worker.last_reconcile, T0, "a reconcile that tripped is a reconcile that ran")
                self.assertEqual(worker.last_success, T0, "and a pass that succeeded: the sync is not failing")

    def test_the_boundaries_are_at_min_and_above_percent(self):
        for n, gone, trips in ((10, 4, False), (10, 5, True), (50, 5, False), (50, 6, True), (100, 10, False), (100, 11, True)):
            with self.subTest(people=n, gone=gone):
                self.setUp()
                people = self.people(n)
                self.kc.gone.update(u["kc"] for u in people[:gone])
                done = self.reconcile(self.sync(30))
                self.assertEqual(done["disabled"], 0 if trips else gone)
                self.assertEqual(len(self.trips()), 1 if trips else 0)
                self.assertEqual(self.gauge(), 1 if trips else 0)

    def test_the_settings_move_the_thresholds(self):
        for breaker, n, gone, trips in (({"keycloak_breaker_min": 2}, 20, 3, True), ({"keycloak_breaker_min": 2}, 20, 1, False),
                                        ({"keycloak_breaker_percent": 50}, 20, 10, False), ({"keycloak_breaker_percent": 50}, 20, 11, True),
                                        ({"keycloak_breaker_min": 1, "keycloak_breaker_percent": 1}, 100, 2, True),
                                        ({"keycloak_breaker_min": 1, "keycloak_breaker_percent": 1}, 100, 1, False)):
            with self.subTest(breaker=breaker, people=n, gone=gone):
                self.setUp()
                people = self.people(n)
                self.kc.gone.update(u["kc"] for u in people[:gone])
                done = self.reconcile(self.sync(30, **breaker))
                self.assertEqual((done["disabled"], len(self.trips())), (0 if trips else gone, 1 if trips else 0))

    def test_the_percent_is_of_the_people_read_not_of_everyone_known(self):
        """Of 60 known people 40 answer 500 (a failing Keycloak), 20 answer, 5 of them gone: 25 % of those read."""
        people = self.people(60)
        failing, answering = people[:40], people[40:]
        self.kc.gone.update(u["kc"] for u in answering[:5])
        calls = self.kc.__call__

        def keycloak(request):
            if request.url.path.rsplit("/", 1)[1] in {u["kc"] for u in failing}:
                return httpx2.Response(500, json={"error": "unknown_error"})
            return calls(request)

        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(keycloak))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))
        done = self.reconcile(self.sync(30))
        self.assertEqual(done["disabled"], 0)
        self.assertEqual((self.trips()[0]["gone"], self.trips()[0]["read"]), (5, 20))

    def test_a_running_grace_period_does_not_advance_while_the_breaker_is_open(self):
        people = self.people(20)
        ada, others = people[0], people[1:]
        self.kc.gone.add(ada["kc"])                                 # an administrator deleted ada: alone, as before
        worker = self.sync(30)
        self.assertEqual(self.reconcile(worker)["disabled"], 1)
        self.assert_cut_off(ada, deleted_at=T0)
        self.kc.gone.update(u["kc"] for u in others[:5])            # 30 days later Keycloak loses five more at once
        done = self.reconcile(worker, T0 + 30 * DAY)
        self.assertEqual((done["disabled"], done["erased"]), (0, 0), "ada's grace period is over, but nothing is erased")
        self.assert_cut_off(ada, deleted_at=T0)
        self.assertEqual(self.trips()[0]["gone"], 5, "ada, found gone before, is not counted again")
        self.assertEqual(self.reconcile(worker, T0 + 60 * DAY)["erased"], 0, "still open: still held")
        self.assertIsNotNone(self.row(ada))

    def test_it_stays_open_until_resolved_and_holds_whoever_goes_missing_meanwhile(self):
        people = self.people(20)
        first, later = people[:5], people[5:7]
        self.kc.gone.update(u["kc"] for u in first)
        worker = self.sync(30)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        self.kc.gone.update(u["kc"] for u in later)                 # two more go missing while the breaker is open
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            for i in range(1, 4):
                done = self.reconcile(worker, T0 + i * 600)
                self.assertEqual((done["disabled"], done["erased"]), (0, 0))
        for user in first + later:
            self.assert_untouched(user)
        self.assertEqual([t["id"] for t in self.trips()], [pass_id], "one trip, however many reconciles pass")
        self.assertEqual(len(self.store.breaker_trip_people(pass_id)), 5, "the pass keeps the people it found")
        held = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.held"]
        self.assertEqual([(r.pass_id, r.held) for r in held], [(pass_id, 7)] * 3, "each reconcile says what it holds")
        self.assertEqual(self.gauge(), 1)

    def test_a_disabled_person_is_still_cut_off_and_a_restored_one_still_noticed_while_open(self):
        """The breaker is about Keycloak's 404. A 200 ``enabled: false`` is Keycloak's own answer about a person it
        has, and reversible (they sign in again once enabled), so it is acted on; a person Keycloak has again stops
        their grace period as before."""
        people = self.people(20)
        self.kc.gone.add(people[0]["kc"])
        worker = self.sync(30)
        self.reconcile(worker)                                      # people[0]: cut off, the clock runs
        self.kc.gone.discard(people[0]["kc"])                       # restored from a backup
        self.kc.gone.update(u["kc"] for u in people[1:6])           # five others gone: the breaker trips
        self.kc.disabled.add(people[10]["kc"])                      # and an administrator disables one
        done = self.reconcile(worker, T0 + 600)
        self.assertEqual((done["disabled"], done["erased"]), (1, 0))
        self.assert_cut_off(people[10])
        self.assertIsNone(self.row(people[10])["deleted_at"])
        self.assertIsNone(self.row(people[0])["deleted_at"], "Keycloak has them again: the clock stops")
        self.assertEqual(len(self.trips()), 1)

    def test_the_events_pass_holds_a_404_too_while_open(self):
        people = self.people(20)
        self.kc.gone.update(u["kc"] for u in people[:5])
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.reconcile(worker)
        bob = people[10]
        self.kc.gone.add(bob["kc"])                                 # an administrator's DELETE is a USER admin event
        self.kc.admin_events.append({"time": int(self.now * 1000) + 1000, "resourceType": "USER", "operationType": "DELETE",
                                     "resourcePath": f"users/{bob['kc']}"})
        done = self.tick(worker, T0 + 2)
        self.assertEqual((done["checked"], done["disabled"]), (1, 0), "read, held")
        self.assert_untouched(bob)
        self.assertEqual(self.tick(worker, T0 + 32)["disabled"], 0, "the look-back reads his event again: still held")
        self.assert_untouched(bob)
        self.assertEqual(self.operator("dismiss-deletions", "--pass", self.trips()[0]["id"])[0], 0)
        done = self.reconcile(worker, T0 + 600)
        self.assertEqual(done["disabled"], 1, "closed: his event, read again, cuts him off; the five trip again")
        self.assert_cut_off(bob, deleted_at=T0 + 600)
        self.assertEqual(self.trips()[1]["gone"], 5, "bob, acted on by the events pass, is not newly gone")

    def test_off_cuts_everyone_off_as_before_and_records_nothing(self):
        people = self.people(20)
        self.kc.gone.update(u["kc"] for u in people[:5])
        done = self.reconcile(self.sync(0, keycloak_breaker=False))
        self.assertEqual((done["disabled"], done["erased"]), (5, 5))
        self.assertEqual(self.trips(), [])
        self.assertEqual(self.gauge(), 0)


class AcrossARestart(Scenario):
    def test_the_trip_persists_and_a_new_worker_keeps_holding(self):
        people = self.people(20)
        self.kc.gone.update(u["kc"] for u in people[:5])
        self.reconcile(self.sync(0))
        pass_id = self.trips()[0]["id"]
        self.store.close()                                          # the process restarts
        self.store = Store(self.database, KEY)
        self.addCleanup(self.store.close)
        worker = self.sync(0)                                       # a new worker, new metrics; the reconcile is due at start
        self.assertEqual(self.gauge(), 1, "the gauge is read from the database: 1 before any pass")
        done = self.reconcile(worker)
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (20, 0, 0))
        for user in people:
            self.assert_untouched(user)
        self.assertEqual([t["id"] for t in self.trips()], [pass_id], "no second trip")
        self.assertEqual(sorted(p["user_id"] for p in self.store.breaker_trip_people(pass_id)),
                         sorted(u["id"] for u in people[:5]))


class Confirmation(Scenario):
    def test_confirming_applies_exactly_that_passes_people_and_audits_it(self):
        for mode, days in MODES.items():
            with self.subTest(mode=mode):
                self.setUp()
                people = self.people(20)
                held, later, present = people[:5], people[5], people[6:]
                self.kc.gone.update(u["kc"] for u in held)
                worker = self.sync(days)
                self.reconcile(worker)
                pass_id = self.trips()[0]["id"]
                self.kc.gone.add(later["kc"])                       # gone after the trip: not in the pass
                self.reconcile(worker, T0 + 600)                    # still open: held with the others
                self.now = T0 + 3600
                with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
                    status, out = self.operator("confirm-deletions", "--pass", pass_id, "--by", "on-call")
                self.assertEqual(status, 0, out)
                for user in held:
                    self.assert_cut_off(user, deleted_at=T0 + 3600)   # the grace period runs from the confirmation
                    self.assertIsNotNone(self.row(user), "not erased by the confirmation itself, in any mode")
                self.assert_untouched(later)
                for user in present:
                    self.assert_untouched(user)
                trip = self.store.get_breaker_trip(pass_id)
                self.assertEqual((trip["status"], trip["resolved_at"], trip["resolved_by"]), ("confirmed", T0 + 3600, "on-call"))
                confirmed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"]
                self.assertEqual(len(confirmed), 1, "one audit line")
                self.assertEqual((confirmed[0].pass_id, confirmed[0].applied, confirmed[0].skipped, confirmed[0].by),
                                 (pass_id, 5, 0, "on-call"))
                cut = [r for r in logs.records if getattr(r, "event", "") == "identity.user.disabled"]
                self.assertEqual(sorted(r.user_id for r in cut), sorted(u["id"] for u in held))
                self.assertEqual({(r.reason, r.via) for r in cut}, {("deleted", "confirm")})
                self.assertEqual(self.gauge(), 0, "closed")
                # The breaker is closed: the next reconcile finds one person newly gone (under the minimum) and the
                # five confirmed with their grace period running, and acts on each as the policy says.
                done = self.reconcile(worker, T0 + 3600 + 600)
                self.assertEqual(done["disabled"], 1)
                self.assertEqual(len(self.trips()), 1, "no new trip")
                if days is None:
                    self.assertEqual(done["erased"], 0)
                    self.assert_cut_off(later, deleted_at=T0 + 3600 + 600)
                    self.assertEqual(self.reconcile(worker, T0 + 400 * DAY)["erased"], 0, "off: never erased")
                elif days == 0:
                    self.assertEqual(done["erased"], 6, "0 days: the five at the first reconcile after the confirmation, "
                                                        "and the one newly found gone at once, as before")
                    for user in held + [later]:
                        self.assert_erased(user)
                else:
                    self.assert_cut_off(later, deleted_at=T0 + 3600 + 600)
                    self.assertEqual(done["erased"], 0)
                    self.assertEqual(self.reconcile(worker, T0 + 3600 + days * DAY - 1)["erased"], 0, "a second short")
                    self.assertEqual(self.reconcile(worker, T0 + 3600 + days * DAY)["erased"], 5, "exactly N days after")
                    for user in held:
                        self.assert_erased(user)
                    self.assertIsNotNone(self.row(later))
                self.assertEqual(self.counted("inkcast_identity_users_deleted_total").get("reconcile", 0),
                                 {None: 0, 0: 6}.get(days, 5), "each erasure counted once, by the reconcile")

    def test_a_person_who_signed_in_since_the_trip_is_spared_and_one_erased_meanwhile_is_skipped(self):
        people = self.people(20)
        held = people[:5]
        self.kc.gone.update(u["kc"] for u in held)
        worker = self.sync(30)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        back = held[0]                                              # Keycloak has them again and lets them in
        self.kc.gone.discard(back["kc"])
        self.assertEqual(self.store.upsert_user(PREFIX + back["kc"], back["kc"], None, None)["id"], back["id"])
        self.kc.events.append({"time": int(self.now * 1000) + 1000, "type": "DELETE_ACCOUNT", "userId": held[1]["kc"]})
        self.assertEqual(self.tick(worker, T0 + 2)["erased"], 1, "held[1] deleted their own account: erased, breaker or not")
        self.assert_erased(held[1])
        self.now = T0 + 3600
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.assertEqual(self.operator("confirm-deletions", "--pass", pass_id)[0], 0)
        self.assert_untouched(back)
        for user in held[2:]:
            self.assert_cut_off(user, deleted_at=T0 + 3600)
        confirmed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"][0]
        self.assertEqual((confirmed.applied, confirmed.skipped), (3, 1), "the erased one is in no pass any more")
        self.assertEqual(self.store.breaker_trip_people(pass_id), [], "a resolved pass keeps no ids")

    def test_the_wrong_pass_or_a_second_confirmation_does_nothing(self):
        people = self.people(20)
        held = people[:5]
        self.kc.gone.update(u["kc"] for u in held)
        worker = self.sync(30)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        for wrong in ("pass_000000000000", "nope", pass_id.upper()):
            with self.subTest(wrong=wrong):
                status, out = self.operator("confirm-deletions", "--pass", wrong)
                self.assertEqual(status, 1)
                for user in held:
                    self.assert_untouched(user)
                self.assertEqual(self.store.get_breaker_trip(pass_id)["status"], "pending")
        self.assertEqual(self.gauge(), 1)
        self.now = T0 + 100
        self.assertEqual(self.operator("confirm-deletions", "--pass", pass_id)[0], 0)
        self.now = T0 + 200
        with self.assertLogs("inkcast", level="INFO") as logs:
            status, out = self.operator("confirm-deletions", "--pass", pass_id)
        self.assertEqual(status, 1, "already confirmed")
        self.assertEqual([r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"], [],
                         "no second audit line")
        for user in held:
            self.assert_cut_off(user, deleted_at=T0 + 100)
        self.assertEqual(self.store.get_breaker_trip(pass_id)["resolved_at"], T0 + 100)
        self.assertEqual(self.operator("dismiss-deletions", "--pass", pass_id)[0], 1, "nor dismissed afterwards")
        self.assertEqual(self.store.get_breaker_trip(pass_id)["status"], "confirmed")

    def test_dismissing_does_nothing_to_anyone_and_the_next_reconcile_reads_everyone_again(self):
        people = self.people(20)
        self.kc.gone.update(u["kc"] for u in people[:5])
        worker = self.sync(0)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        self.now = T0 + 60
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            status, out = self.operator("dismiss-deletions", "--pass", pass_id, "--by", "on-call")
        self.assertEqual(status, 0, out)
        for user in people:
            self.assert_untouched(user)
        trip = self.store.get_breaker_trip(pass_id)
        self.assertEqual((trip["status"], trip["resolved_at"], trip["resolved_by"]), ("dismissed", T0 + 60, "on-call"))
        dismissed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.dismissed"]
        self.assertEqual([(r.pass_id, r.by) for r in dismissed], [(pass_id, "on-call")])
        self.assertEqual(self.gauge(), 0)
        self.kc.gone.clear()                                        # Keycloak was fixed: everyone is back
        self.assertEqual(self.reconcile(worker, T0 + 600), {"checked": 20, "disabled": 0, "erased": 0})
        self.assertEqual(len(self.trips()), 1)
        self.kc.gone.update(u["kc"] for u in people[:5])            # not fixed after all: it trips again, a new pass
        self.reconcile(worker, T0 + 1200)
        for user in people:
            self.assert_untouched(user)
        self.assertEqual([t["status"] for t in self.trips()], ["dismissed", "pending"])
        self.assertNotEqual(self.trips()[1]["id"], pass_id)
        self.assertEqual(self.gauge(), 1)

    def test_trips_lists_every_pass_with_its_status(self):
        self.assertEqual(self.operator("trips"), (0, ""))
        people = self.people(20)
        self.kc.gone.update(u["kc"] for u in people[:5])
        self.reconcile(self.sync(30))
        pass_id = self.trips()[0]["id"]
        status, out = self.operator("trips")
        self.assertEqual(status, 0)
        self.assertIn(pass_id, out)
        self.assertIn("pending", out)
        self.assertIn("5 of 20", out)
        self.assertEqual(self.operator("confirm-deletions", "--pass", pass_id)[0], 0)
        self.assertIn("confirmed", self.operator("trips")[1])
        for user in people[:5]:
            self.assertNotIn(user["id"], self.operator("trips")[1], "ids are not printed: the pass is what matters")


class SelfDeletions(Scenario):
    def test_a_delete_account_event_still_erases_while_the_breaker_is_open(self):
        for mode, days in MODES.items():
            with self.subTest(mode=mode):
                self.setUp()
                people = self.people(20)
                held, carol, dave = people[:5], people[5], people[6]
                self.kc.gone.update(u["kc"] for u in held)
                worker = self.sync(days)
                self.assertEqual(self.tick(worker)["erased"], 0, "the cursors are set")
                self.reconcile(worker)
                self.assertEqual(len(self.trips()), 1)
                self.kc.gone.update({carol["kc"], dave["kc"]})     # both delete their own accounts
                self.kc.events.append({"time": int(self.now * 1000) + 1000, "type": "DELETE_ACCOUNT", "userId": carol["kc"]})
                self.assertEqual(self.tick(worker, T0 + 2)["erased"], 1, "carol: by the events pass, breaker open")
                self.assert_erased(carol)
                # dave's event is stamped before the cursor's look-back: only the reconcile finds it.
                self.store.set_sync_state(USER_EVENTS_CURSOR, str(int(self.now * 1000) + 200_000))
                self.kc.events.append({"time": int(self.now * 1000), "type": "DELETE_ACCOUNT", "userId": dave["kc"]})
                done = self.reconcile(worker, T0 + 600)
                self.assertEqual((done["disabled"], done["erased"]), (0, 1), "dave: by the reconcile, breaker open")
                self.assert_erased(dave)
                for user in held:
                    self.assert_untouched(user)
                self.assertEqual(len(self.trips()), 1, "still the one trip, still pending")
                self.assertEqual(self.gauge(), 1)
                self.assertEqual(self.counted("inkcast_identity_users_deleted_total"), {"event": 1, "reconcile": 1})

    def test_self_deletions_the_reconcile_finds_do_not_count_towards_the_trip(self):
        people = self.people(20)
        asked, deleted = people[:3], people[3:7]                    # three with an event, four without: 4 < 5
        self.kc.gone.update(u["kc"] for u in asked + deleted)
        for user in asked:
            self.kc.events.append({"time": int(T0 * 1000) - 300_000, "type": "DELETE_ACCOUNT", "userId": user["kc"]})
        worker = self.sync(30)
        self.store.set_sync_state(USER_EVENTS_CURSOR, str(int(T0 * 1000)))
        done = self.reconcile(worker)
        self.assertEqual((done["disabled"], done["erased"]), (4, 3), "7 gone, 3 of them asked: the breaker counts 4")
        self.assertEqual(self.trips(), [])
        for user in asked:
            self.assert_erased(user)
        for user in deleted:
            self.assert_cut_off(user, deleted_at=T0)


class Configuration(unittest.TestCase):
    BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": KEY,
            "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}

    def test_the_defaults_are_on_10_percent_and_5(self):
        cfg = load_config(self.BASE)
        self.assertEqual((cfg.keycloak_breaker, cfg.keycloak_breaker_percent, cfg.keycloak_breaker_min), (True, 10, 5))
        cfg = load_config({**self.BASE, "INKCAST_KEYCLOAK_BREAKER": "", "INKCAST_KEYCLOAK_BREAKER_PERCENT": "",
                           "INKCAST_KEYCLOAK_BREAKER_MIN": ""})
        self.assertEqual((cfg.keycloak_breaker, cfg.keycloak_breaker_percent, cfg.keycloak_breaker_min), (True, 10, 5))
        cfg.validate()

    def test_each_setting_is_read_and_validated_naming_the_variable(self):
        for raw, on in (("on", True), ("ON", True), (" On ", True), ("off", False), ("OFF", False), ("Off", False)):
            with self.subTest(raw=raw):
                cfg = load_config({**self.BASE, "INKCAST_KEYCLOAK_BREAKER": raw})
                self.assertEqual(cfg.keycloak_breaker, on)
                cfg.validate()
        for raw in ("true", "false", "yes", "no", "1", "0", "enabled", "of", "on/off"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, "INKCAST_KEYCLOAK_BREAKER must be 'on' or 'off'"):
                    load_config({**self.BASE, "INKCAST_KEYCLOAK_BREAKER": raw})
        for raw, percent in (("1", 1), ("10", 10), (" 50 ", 50), ("100", 100)):
            with self.subTest(raw=raw):
                cfg = load_config({**self.BASE, "INKCAST_KEYCLOAK_BREAKER_PERCENT": raw})
                self.assertEqual(cfg.keycloak_breaker_percent, percent)
                cfg.validate()
        for raw in ("0", "101", "-1", "7.5", "ten", "10%", "1e1", "off"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, "INKCAST_KEYCLOAK_BREAKER_PERCENT must be a whole number from 1 to 100"):
                    load_config({**self.BASE, "INKCAST_KEYCLOAK_BREAKER_PERCENT": raw})
        for raw, minimum in (("1", 1), ("5", 5), (" 50 ", 50), ("100000", 100000)):
            with self.subTest(raw=raw):
                cfg = load_config({**self.BASE, "INKCAST_KEYCLOAK_BREAKER_MIN": raw})
                self.assertEqual(cfg.keycloak_breaker_min, minimum)
                cfg.validate()
        for raw in ("0", "-1", "2.5", "five", "off", "none"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, "INKCAST_KEYCLOAK_BREAKER_MIN must be a whole number, 1 or more"):
                    load_config({**self.BASE, "INKCAST_KEYCLOAK_BREAKER_MIN": raw})

    def test_a_config_built_by_hand_is_checked_by_validate(self):
        good = Config(public_url="https://inkcast.example.com", secret_key=KEY, database_path=":memory:",
                      login="password", password="p" * 12)
        dataclasses.replace(good, keycloak_breaker=False, keycloak_breaker_percent=100, keycloak_breaker_min=1).validate()
        for field, value, rule in (("keycloak_breaker_percent", 0, "INKCAST_KEYCLOAK_BREAKER_PERCENT"),
                                   ("keycloak_breaker_percent", 101, "INKCAST_KEYCLOAK_BREAKER_PERCENT"),
                                   ("keycloak_breaker_percent", 7.5, "INKCAST_KEYCLOAK_BREAKER_PERCENT"),
                                   ("keycloak_breaker_percent", "10", "INKCAST_KEYCLOAK_BREAKER_PERCENT"),
                                   ("keycloak_breaker_min", 0, "INKCAST_KEYCLOAK_BREAKER_MIN"),
                                   ("keycloak_breaker_min", True, "INKCAST_KEYCLOAK_BREAKER_MIN"),
                                   ("keycloak_breaker", "on", "INKCAST_KEYCLOAK_BREAKER"),
                                   ("keycloak_breaker", 1, "INKCAST_KEYCLOAK_BREAKER")):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ConfigError, rule):
                    dataclasses.replace(good, **{field: value}).validate()


class Posture(unittest.TestCase):
    def check(self, check_id: str, environment: str, **cfg):
        from inkcast.libs.secrets import Redactor, run_checks
        config = Config(public_url="https://inkcast.example.com", secret_key=KEY, database_path=":memory:",
                        environment=environment, **cfg)
        return {c.id: c for c in run_checks(config, redactor=Redactor(), environ={})}[check_id]

    SYNC = {"login": "oidc", "oidc_issuer": ISSUER, "keycloak_sync_client_id": "inkcast-sync"}

    def test_breaker_off_warns_in_production_and_staging_and_is_only_noted_elsewhere(self):
        for environment, status in (("production", "warn"), ("staging", "warn"), ("ci", "info"), ("development", "info")):
            with self.subTest(environment=environment):
                check = self.check("keycloak-breaker", environment, keycloak_breaker=False, **self.SYNC)
                self.assertEqual(check.status, status)
                self.assertIn("INKCAST_KEYCLOAK_BREAKER=off", check.detail)
                self.assertIn("at once", check.detail)

    def test_breaker_on_passes_with_the_sync_and_says_the_thresholds(self):
        for environment in ("production", "staging", "development"):
            with self.subTest(environment=environment):
                check = self.check("keycloak-breaker", environment, keycloak_breaker_min=7, keycloak_breaker_percent=20, **self.SYNC)
                self.assertEqual(check.status, "pass")
                self.assertIn("INKCAST_KEYCLOAK_BREAKER=on", check.detail)
                self.assertIn("7", check.detail)
                self.assertIn("20", check.detail)
        for cfg in ({"login": "password"}, {"login": "oidc", "oidc_issuer": ISSUER}, {"login": "github"}):
            with self.subTest(cfg=cfg):
                self.assertEqual(self.check("keycloak-breaker", "production", **cfg).status, "info", "not in use")

    def test_erase_after_is_info_not_pass_when_sign_in_is_not_keycloak_or_the_sync_is_off(self):
        for cfg in ({"login": "password"}, {"login": "github"}, {"login": "oidc", "oidc_issuer": ISSUER}):
            for days in (30, 0):
                with self.subTest(cfg=cfg, days=days):
                    check = self.check("keycloak-erase-after", "production", keycloak_erase_after_days=days, **cfg)
                    self.assertEqual(check.status, "info")
                    self.assertIn(f"INKCAST_KEYCLOAK_ERASE_AFTER_DAYS={days}", check.detail)
        self.assertEqual(self.check("keycloak-erase-after", "production", keycloak_erase_after_days=30, **self.SYNC).status, "pass")

    def test_a_very_large_number_of_days_warns_as_effectively_off(self):
        for days in (3651, 10_000, 365 * 100):
            for environment, status in (("production", "warn"), ("staging", "warn"), ("development", "info")):
                with self.subTest(days=days, environment=environment):
                    check = self.check("keycloak-erase-after", environment, keycloak_erase_after_days=days, **self.SYNC)
                    self.assertEqual(check.status, status)
                    self.assertIn(f"INKCAST_KEYCLOAK_ERASE_AFTER_DAYS={days}", check.detail)
                    self.assertIn("effectively off", check.detail)
        self.assertEqual(self.check("keycloak-erase-after", "production", keycloak_erase_after_days=3650, **self.SYNC).status, "pass")

    def test_the_launcher_logs_the_breaker_warning_once(self):
        from inkcast.launcher import gate, resolve
        config = Config(public_url="https://inkcast.example.com", secret_key=KEY, database_path=":memory:",
                        environment="staging", login="password", password="p" * 12, log_format="json",
                        keycloak_breaker=False, authz_allow_local=True)
        with self.assertLogs("inkcast", level="WARNING") as logs:
            gate(config, resolve("IdentityService"))
        warned = [r for r in logs.records if getattr(r, "check", "") == "keycloak-breaker"]
        self.assertEqual(len(warned), 1)
        self.assertEqual(warned[0].event, "config.posture.warn")


class TheAlert(unittest.TestCase):
    def test_a_critical_alert_on_the_gauge_with_a_runbook(self):
        from inkcast.libs.alerting import ALERTS
        alert = {a.name: a for a in ALERTS}["InkcastIdentitySyncBreakerOpen"]
        self.assertEqual(alert.severity, "critical")
        self.assertIn("inkcast_identity_sync_breaker_open", alert.expr)
        self.assertIn("confirm-deletions", alert.description)
        self.assertTrue(alert.runbook.endswith("#inkcastidentitysyncbreakeropen"))


class Migration(unittest.TestCase):
    def test_an_erased_person_leaves_every_pending_pass(self):
        from inkcast.libs.db.erasure import USER_TABLES
        self.assertIn("sync_breaker_people", dict(USER_TABLES))
        store = Store(":memory:", KEY)
        self.addCleanup(store.close)
        ada, bob = (store.upsert_user(f"{PREFIX}{n}", n, None, None) for n in ("ada", "bob"))
        pass_id = store.record_breaker_trip(time.time(), 2, 10, [(ada["id"], ada["last_login_at"]), (bob["id"], bob["last_login_at"])])
        self.assertEqual(store.pending_breaker_trip()["id"], pass_id)
        self.assertEqual(store.erase_user(ada["id"])["sync_breaker_people"], 1)
        self.assertEqual([p["user_id"] for p in store.breaker_trip_people(pass_id)], [bob["id"]])
        self.assertTrue(store.resolve_breaker_trip(pass_id, "confirmed", time.time(), "on-call"))
        self.assertFalse(store.resolve_breaker_trip(pass_id, "dismissed", time.time(), "on-call"), "claimed once")
        self.assertIsNone(store.pending_breaker_trip())


if __name__ == "__main__":
    unittest.main()
