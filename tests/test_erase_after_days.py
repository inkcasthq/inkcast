# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""E9.F9.5.S6 (issue #86): a person gone from Keycloak is erased after INKCAST_KEYCLOAK_ERASE_AFTER_DAYS.

Keycloak answers 404 for a person an administrator deleted and for one who deleted themselves while the sync
missed the event (down longer than the realm's event retention, or the event became visible out of order). Until
this story the reconcile only cut such a person off, so the deletion promise depended on an event being seen. Now
the reconcile re-checks the cut-off people too, and erases a person still gone N days after the cut-off
(``via="reconcile"``): ``0`` at the first reconcile that finds them gone, ``off`` never. A disabled-but-present
Keycloak user is never erased, in any mode; a person Keycloak has again (restored from a backup under the same id)
gets a whole grace period at a later deletion. A found DELETE_ACCOUNT event erases at once in every mode (#87).

The unit tests drive ``KeycloakSync`` over an in-memory store and a mock admin API with a clock of their own, so a
grace period of days passes in a line. The end-to-end test runs the fake Keycloak of ``tests/fakes.py`` (whose
user-events endpoint must honour the ``user`` filter the reconcile relies on)."""

import asyncio
import dataclasses
import os
import sqlite3
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.config import Config, ConfigError, load_config
from inkcast.libs.db import Store
from inkcast.libs.db.erasure import user_hash
from inkcast.libs.secrets import SecretValue
from inkcast.services.identity.keycloak_sync import USER_EVENTS_CURSOR, KeycloakSync
from test_e2e import Harness

ISSUER = "https://kc.example/realms/inkcast"
DAY = 86400
T0 = 1_700_000_000.0
MODES = {"30 days": 30, "0 days": 0, "off": None}


class Keycloak:
    """A Keycloak admin API over ``httpx2.MockTransport``: who is gone (404 ``User not found``), who is disabled, the
    USER admin events and the DELETE_ACCOUNT user events (filtered by type and user, newest first, like
    ``RealmAdminResource.getEvents``), and which people the sync read."""

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
    """One store, one mock Keycloak, a clock the test moves; ``sync(days)`` is a worker in one mode."""

    def setUp(self):
        self.kc = Keycloak()
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.now = T0
        self.counted: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.kc))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))

    def person(self, name: str) -> dict:
        user = self.store.upsert_user(f"oidc:{ISSUER}#kc-{name}", name, f"{name}@example.com", name.title())
        self.store.save_token(f"ic_at_{name:x<43}", "access", "c1", user["id"], ["inkcast"], None, f"f-{name}", 10 * DAY)
        return user

    def sync(self, days: "int | None") -> KeycloakSync:
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="oidc",
                     oidc_issuer=ISSUER, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                     oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync",
                     keycloak_sync_secret=SecretValue("sync-secret"), keycloak_erase_after_days=days)
        cfg.validate()
        scenario = self

        class Metrics:
            def inc(self, name, amount=1, **labels):
                scenario.counted.append((name, tuple(sorted(labels.items()))))

        worker = KeycloakSync(cfg, self.store, self.http, Metrics(), clock=lambda: self.now)
        worker.last_reconcile = self.now                            # a pass is the events path alone until reconcile()
        return worker

    def tick(self, worker: KeycloakSync) -> dict:
        return asyncio.run(worker.tick())

    def reconcile(self, worker: KeycloakSync, at: "float | None" = None) -> dict:
        if at is not None:
            self.now = at
        worker.last_reconcile = 0
        return self.tick(worker)

    def row(self, user: dict) -> "dict | None":
        return self.store.get_user(user["id"])

    def erasures(self) -> list[str]:
        return [r["user_hash"] for r in self.store.user_erasures()]

    def erased_via(self) -> list[str]:
        return [dict(labels)["via"] for name, labels in self.counted if name == "inkcast_identity_users_deleted_total"]

    def assert_cut_off(self, user: dict, name: str) -> None:
        row = self.row(user)
        self.assertIsNotNone(row, f"{name} keeps their row")
        self.assertIsNotNone(row["disabled_at"], f"{name} is cut off")
        self.assertIsNone(self.store.get_token(f"ic_at_{name:x<43}", "access"), f"{name}'s token is dead")

    def assert_erased(self, user: dict, name: str, times: int = 1) -> None:
        self.assertIsNone(self.row(user), f"{name} is erased")
        self.assertEqual(self.erasures().count(user_hash(self.store._erasure_salt, user["id"])), times,
                         f"{name}: one audit record")


class EachMode(Scenario):
    def test_n_days_cuts_off_now_and_erases_on_the_nth_day(self):
        ada = self.person("ada")
        worker = self.sync(30)
        self.kc.gone.add("kc-ada")                                  # an administrator deleted her: no event
        done = self.reconcile(worker)
        self.assertEqual((done["disabled"], done["erased"]), (1, 0))
        self.assert_cut_off(ada, "ada")
        self.assertEqual(self.row(ada)["deleted_at"], T0, "the grace period runs from the cut-off")
        self.assertEqual(self.reconcile(worker, T0 + 29 * DAY)["erased"], 0, "still inside the grace period")
        self.assert_cut_off(ada, "ada")
        done = self.reconcile(worker, T0 + 30 * DAY)
        self.assertEqual((done["disabled"], done["erased"]), (0, 1), "30 days later, still gone: erased")
        self.assert_erased(ada, "ada")
        self.assertEqual(self.erased_via(), ["reconcile"])

    def test_zero_erases_at_the_first_reconcile_that_finds_them_gone(self):
        ada, bob = self.person("ada"), self.person("bob")
        worker = self.sync(0)
        self.assertEqual(self.reconcile(worker)["erased"], 0, "both present: nothing")
        self.kc.gone.add("kc-ada")
        self.assertEqual(self.reconcile(worker)["erased"], 1)
        self.assert_erased(ada, "ada")
        self.assertIsNotNone(self.row(bob), "bob is untouched")
        self.assertIsNone(self.row(bob)["disabled_at"])
        self.assertEqual(self.erased_via(), ["reconcile"])

    def test_off_cuts_off_and_never_erases_on_a_404(self):
        ada = self.person("ada")
        worker = self.sync(None)
        self.kc.gone.add("kc-ada")
        self.assertEqual(self.reconcile(worker)["disabled"], 1)
        self.assert_cut_off(ada, "ada")
        for days in (1, 30, 31, 365 * 10):
            self.assertEqual(self.reconcile(worker, T0 + days * DAY)["erased"], 0, f"{days} days later: not erased")
        self.assert_cut_off(ada, "ada")
        self.assertEqual(self.erased_via(), [])
        self.assertEqual(self.erasures(), [])
        self.assertEqual(self.row(ada)["deleted_at"], T0, "recorded all the same: a later setting can use it")

    def test_the_boundary_is_exactly_n_days(self):
        for days in (1, 7, 30):
            with self.subTest(days=days):
                self.setUp()
                ada = self.person("ada")
                worker = self.sync(days)
                self.kc.gone.add("kc-ada")
                self.reconcile(worker)
                self.assertEqual(self.reconcile(worker, T0 + days * DAY - 1)["erased"], 0, "a second short: not yet")
                self.assertIsNotNone(self.row(ada))
                self.assertEqual(self.reconcile(worker, T0 + days * DAY)["erased"], 1, "exactly N days: erased")
                self.assert_erased(ada, "ada")

    def test_a_disabled_but_present_person_is_never_erased_in_any_mode(self):
        for mode, days in MODES.items():
            with self.subTest(mode=mode):
                self.setUp()
                ada = self.person("ada")
                worker = self.sync(days)
                self.kc.disabled.add("kc-ada")                      # an administrator disabled her
                done = self.reconcile(worker)
                self.assertEqual((done["disabled"], done["erased"]), (1, 0))
                self.assert_cut_off(ada, "ada")
                for later in (0, 1, 30, 365):
                    self.assertEqual(self.reconcile(worker, T0 + later * DAY)["erased"], 0, f"{later} days later")
                self.assert_cut_off(ada, "ada")
                self.assertIsNone(self.row(ada)["deleted_at"], "Keycloak has her: no grace period runs")
                self.assertEqual(self.counted.count(("inkcast_identity_users_disabled_total", (("reason", "disabled"),))), 1,
                                 "cut off once, however often the reconcile sees her disabled")
                self.assertEqual(self.erased_via(), [])
                self.assertEqual(self.erasures(), [])

    def test_counted_and_audited_once_across_repeated_reconciles(self):
        ada = self.person("ada")
        worker = self.sync(2)
        self.kc.gone.add("kc-ada")
        for i in range(5):
            self.reconcile(worker, T0 + i * DAY)                    # days 0 and 1: cut off; day 2: erased; 3, 4: nothing
        self.assert_erased(ada, "ada")
        self.assertEqual(self.erased_via(), ["reconcile"], "one increment, not one per reconcile")
        self.assertEqual(self.counted.count(("inkcast_identity_users_disabled_total", (("reason", "deleted"),))), 1)
        self.assertEqual(self.kc.read.count("kc-ada"), 3, "read while she had a row, then no more")


class TheClock(Scenario):
    def test_the_grace_period_runs_from_the_deletion_not_from_an_earlier_disable(self):
        """Disabled at T0 (cut off then), deleted by an administrator 40 days later: erased 30 days after the
        deletion, not at once because disabled_at is older than the grace period."""
        ada = self.person("ada")
        worker = self.sync(30)
        self.kc.disabled.add("kc-ada")
        self.assertEqual(self.reconcile(worker)["disabled"], 1)
        self.kc.gone.add("kc-ada")                                  # 40 days later: deleted
        done = self.reconcile(worker, T0 + 40 * DAY)
        self.assertEqual((done["disabled"], done["erased"]), (0, 0), "already cut off; the grace period starts now")
        self.assertEqual(self.row(ada)["deleted_at"], T0 + 40 * DAY)
        self.assertEqual(self.reconcile(worker, T0 + 69 * DAY)["erased"], 0)
        self.assertEqual(self.reconcile(worker, T0 + 70 * DAY)["erased"], 1)
        self.assert_erased(ada, "ada")

    def test_a_person_keycloak_has_again_gets_a_whole_grace_period_at_a_later_deletion(self):
        """Deleted by mistake at T0 and restored from a backup under the same id 10 days later (she has not signed
        in yet, so she stays cut off); deleted again on day 29: not erased on day 30, erased on day 59."""
        ada = self.person("ada")
        worker = self.sync(30)
        self.kc.gone.add("kc-ada")
        self.reconcile(worker)
        self.assertEqual(self.row(ada)["deleted_at"], T0)
        self.kc.gone.discard("kc-ada")                              # restored
        done = self.reconcile(worker, T0 + 10 * DAY)
        self.assertEqual((done["disabled"], done["erased"]), (0, 0))
        row = self.row(ada)
        self.assertIsNone(row["deleted_at"], "Keycloak has her: the clock stops")
        self.assertIsNotNone(row["disabled_at"], "still cut off until she signs in")
        self.kc.gone.add("kc-ada")                                  # deleted again on day 29
        self.reconcile(worker, T0 + 29 * DAY)
        self.assertEqual(self.row(ada)["deleted_at"], T0 + 29 * DAY)
        self.assertEqual(self.reconcile(worker, T0 + 30 * DAY)["erased"], 0, "one day into the new grace period")
        self.assertEqual(self.reconcile(worker, T0 + 59 * DAY)["erased"], 1)
        self.assert_erased(ada, "ada")

    def test_a_restore_that_is_still_disabled_stops_the_clock_too(self):
        ada = self.person("ada")
        worker = self.sync(30)
        self.kc.gone.add("kc-ada")
        self.reconcile(worker)
        self.kc.gone.discard("kc-ada")
        self.kc.disabled.add("kc-ada")                              # restored, left disabled
        self.reconcile(worker, T0 + 10 * DAY)
        self.assertIsNone(self.row(ada)["deleted_at"])
        for later in (30, 31, 400):
            self.assertEqual(self.reconcile(worker, T0 + later * DAY)["erased"], 0, "present but disabled: never erased")
        self.assert_cut_off(ada, "ada")

    def test_signing_in_again_clears_the_clock_with_the_cut_off(self):
        ada = self.person("ada")
        worker = self.sync(30)
        self.kc.gone.add("kc-ada")
        self.reconcile(worker)
        self.kc.gone.discard("kc-ada")
        back = self.store.upsert_user(f"oidc:{ISSUER}#kc-ada", "ada", None, None)    # Keycloak let her sign in
        self.assertEqual(back["id"], ada["id"])
        self.assertEqual((self.row(ada)["disabled_at"], self.row(ada)["deleted_at"]), (None, None))
        self.assertEqual(self.reconcile(worker, T0 + 365 * DAY)["erased"], 0)

    def test_the_admin_event_path_starts_the_clock_and_the_reconcile_erases(self):
        """An administrator's DELETE is a USER admin event: the events pass cuts the person off and records when
        they were found gone; the reconcile erases them N days after that, via="reconcile"."""
        ada = self.person("ada")
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursor is set")
        self.kc.gone.add("kc-ada")
        self.kc.admin_events.append({"time": int(self.now * 1000) + 1, "resourceType": "USER", "operationType": "DELETE",
                                     "resourcePath": "users/kc-ada"})
        self.now += 1
        done = self.tick(worker)
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (1, 1, 0))
        self.assertEqual(self.row(ada)["deleted_at"], self.now)
        self.assertEqual(self.reconcile(worker, self.now + 30 * DAY - 1)["erased"], 0)
        self.assertEqual(self.reconcile(worker, self.now + 1)["erased"], 1)
        self.assert_erased(ada, "ada")
        self.assertEqual(self.erased_via(), ["reconcile"])

    def test_people_cut_off_before_this_story_join_the_grace_period_at_their_next_reconcile(self):
        """A row cut off (reason deleted) before the deleted_at column existed has disabled_at only. The reconcile,
        which used to skip disabled people, now re-checks them: the grace period starts at that reconcile, not at
        the old disabled_at (whose reason nobody recorded)."""
        ada = self.person("ada")
        self.store.disable_user(ada["id"])
        self.store._exec("UPDATE users SET disabled_at = ?, deleted_at = NULL WHERE id = ?", T0 - 100 * DAY, ada["id"])
        worker = self.sync(30)
        self.kc.gone.add("kc-ada")
        done = self.reconcile(worker)
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (1, 0, 0), "re-checked, not cut off twice")
        self.assertEqual(self.row(ada)["deleted_at"], T0)
        self.assertEqual(self.reconcile(worker, T0 + 29 * DAY)["erased"], 0)
        self.assertEqual(self.reconcile(worker, T0 + 30 * DAY)["erased"], 1)
        self.assert_erased(ada, "ada")


class SelfDeletionsTheEventPassMissed(Scenario):
    def test_a_delete_account_event_found_by_the_reconcile_erases_at_once_in_every_mode(self):
        """Issue #87's path, now labelled via="reconcile": the person deleted the account themselves, the event was
        missed (here: stamped before the cursor's look-back), and Keycloak still holds it. No grace period applies,
        not even with the setting off: that setting is about a 404 with no event."""
        for mode, days in MODES.items():
            with self.subTest(mode=mode):
                self.setUp()
                ada, bob = self.person("ada"), self.person("bob")
                worker = self.sync(days)
                self.store.set_sync_state(USER_EVENTS_CURSOR, str(int(self.now * 1000) + 120_000))   # a replica ran fast
                self.kc.gone.update({"kc-ada", "kc-bob"})
                self.kc.events.append({"time": int(self.now * 1000), "type": "DELETE_ACCOUNT", "userId": "kc-ada"})
                self.assertEqual(self.tick(worker)["erased"], 0, "the events pass is past her event")
                done = self.reconcile(worker)
                self.assertEqual(done["erased"], 1 if days != 0 else 2)
                self.assert_erased(ada, "ada")
                self.assertEqual(self.erased_via()[0], "reconcile")
                if days is None:
                    self.assert_cut_off(bob, "bob")
                    self.assertEqual(self.reconcile(worker, T0 + 400 * DAY)["erased"], 0, "bob: no event, never erased")
                elif days:
                    self.assert_cut_off(bob, "bob")
                    self.assertEqual(self.reconcile(worker, T0 + days * DAY)["erased"], 1)
                    self.assert_erased(bob, "bob")
                else:
                    self.assert_erased(bob, "bob")


class Configuration(unittest.TestCase):
    BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
            "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}

    def test_the_default_is_30_days_and_0_and_off_are_read(self):
        self.assertEqual(load_config(self.BASE).keycloak_erase_after_days, 30)
        self.assertEqual(load_config({**self.BASE, "INKCAST_KEYCLOAK_ERASE_AFTER_DAYS": ""}).keycloak_erase_after_days, 30)
        for raw, days in (("0", 0), ("7", 7), (" 90 ", 90), ("off", None), ("OFF", None), ("Off", None)):
            with self.subTest(raw=raw):
                cfg = load_config({**self.BASE, "INKCAST_KEYCLOAK_ERASE_AFTER_DAYS": raw})
                self.assertEqual(cfg.keycloak_erase_after_days, days)
                cfg.validate()

    def test_anything_else_is_a_configuration_error_naming_the_variable(self):
        for raw in ("-1", "-30", "7.5", "thirty", "never", "on", "30d", "0x1e", "1e2", "none", "false", "∞"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, "INKCAST_KEYCLOAK_ERASE_AFTER_DAYS must be a whole number of days"):
                    load_config({**self.BASE, "INKCAST_KEYCLOAK_ERASE_AFTER_DAYS": raw})

    def test_a_config_built_by_hand_is_checked_by_validate(self):
        good = Config(public_url="https://inkcast.example.com", secret_key="Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
                      database_path=":memory:", login="password", password="p" * 12)
        for days in (30, 0, None):
            dataclasses.replace(good, keycloak_erase_after_days=days).validate()
        for days in (-1, 7.5, "30", "off"):
            with self.subTest(days=days):
                with self.assertRaisesRegex(ConfigError, "INKCAST_KEYCLOAK_ERASE_AFTER_DAYS must be a whole number of days"):
                    dataclasses.replace(good, keycloak_erase_after_days=days).validate()


class Posture(unittest.TestCase):
    def check(self, days, environment: str):
        from inkcast.libs.secrets import Redactor, run_checks
        # With the sync in use (Keycloak sign-in and a service account): elsewhere a number of days is only noted (#105).
        config = Config(public_url="https://inkcast.example.com", secret_key="Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
                        database_path=":memory:", environment=environment, keycloak_erase_after_days=days, login="oidc",
                        oidc_issuer=ISSUER, keycloak_sync_client_id="inkcast-sync")
        return {c.id: c for c in run_checks(config, redactor=Redactor(), environ={})}["keycloak-erase-after"]

    def test_off_warns_in_production_and_staging_and_is_only_noted_elsewhere(self):
        for environment, status in (("production", "warn"), ("staging", "warn"), ("ci", "info"), ("development", "info")):
            with self.subTest(environment=environment):
                check = self.check(None, environment)
                self.assertEqual(check.status, status)
                self.assertIn("INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=off", check.detail)
                self.assertIn("cut off", check.detail)

    def test_a_number_of_days_passes_and_says_when(self):
        for environment in ("production", "staging", "development"):
            for days, said in ((30, "30 day(s) after the cut-off"), (1, "1 day(s)"), (0, "at the first reconcile")):
                with self.subTest(environment=environment, days=days):
                    check = self.check(days, environment)
                    self.assertEqual(check.status, "pass")
                    self.assertIn(f"INKCAST_KEYCLOAK_ERASE_AFTER_DAYS={days}", check.detail)
                    self.assertIn(said, check.detail)

    def test_the_launcher_logs_the_warning_once(self):
        from inkcast.launcher import gate, resolve
        config = Config(public_url="https://inkcast.example.com", secret_key="Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
                        database_path=":memory:", environment="staging", login="password", password="p" * 12,
                        log_format="json", keycloak_erase_after_days=None, authz_allow_local=True)
        with self.assertLogs("inkcast", level="WARNING") as logs:
            gate(config, resolve("IdentityService"))
        warned = [r for r in logs.records if getattr(r, "check", "") == "keycloak-erase-after"]
        self.assertEqual(len(warned), 1)
        self.assertEqual(warned[0].event, "config.posture.warn")


class Migration(unittest.TestCase):
    def test_an_existing_database_gains_the_deleted_at_column(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)                      # a users table from before this story: disabled_at only
        old.execute("CREATE TABLE users (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, display TEXT NOT NULL,"
                    " email TEXT, name TEXT, created_at REAL NOT NULL, last_login_at REAL NOT NULL, disabled_at REAL)")
        old.execute("INSERT INTO users VALUES ('usr_1', 'oidc:x#1', 'ada', NULL, NULL, 1, 1, 2)")
        old.commit()
        old.close()
        store = Store(path, "k" * 40)
        self.addCleanup(store.close)
        row = store.find_user_by_identity("oidc:x#1")
        self.assertEqual((row["disabled_at"], row["deleted_at"]), (2, None), "cut off before; no grace period running")
        self.assertEqual([u["id"] for u in store.users_with_identity_prefix("oidc:x#", include_disabled=True)], ["usr_1"])
        self.assertEqual(store.users_with_identity_prefix("oidc:x#"), [], "the default still leaves disabled people out")
        self.assertEqual(store.mark_user_deleted("usr_1", 10.0), 10.0)
        self.assertEqual(store.mark_user_deleted("usr_1", 20.0), 10.0, "the first sighting is kept")
        store.mark_user_present("usr_1")
        self.assertIsNone(store.find_user_by_identity("oidc:x#1")["deleted_at"])
        Store(path, "k" * 40).close()                    # opening again is a no-op, not an error


class EndToEnd(Harness):
    """Against the fake Keycloak: carol deletes her account from the account console (a DELETE_ACCOUNT event), an
    administrator deletes dave (an admin event, no user event). The events pass erases carol and cuts dave off; the
    reconcile 30 days later erases dave. The fake's user-events endpoint honours the ``user`` filter: asked about
    dave it answers nothing, however many other people deleted their accounts."""

    extra_config = {"keycloak_sync_client_id": "inkcast-sync", "keycloak_sync_secret": "sync-secret"}

    def test_an_administrators_deletion_is_erased_after_the_grace_period(self):
        base, first = self.config.public_url, dict(self.fakes.next_user)
        self.addCleanup(setattr, self.fakes, "next_user", first)
        tokens = {}
        for sub in ("kc-ead-carol", "kc-ead-dave"):
            self.fakes.next_user = {**first, "sub": sub, "preferred_username": sub, "email": f"{sub}@example.com"}
            tokens[sub] = self.access_token()
        identity = lambda sub: self.app.store.find_user_by_identity(f"oidc:{self.fakes.issuer}#{sub}")
        dave = identity("kc-ead-dave")

        async def me(sub):
            async with httpx2.AsyncClient() as c:
                return (await c.get(f"{base}/api/v1/me", headers={"Authorization": f"Bearer {tokens[sub]}"})).status_code

        async def events_for(sub) -> list[str]:
            async with httpx2.AsyncClient() as c:
                r = await c.post(f"{self.fakes.issuer}/protocol/openid-connect/token", data={
                    "grant_type": "client_credentials", "client_id": "inkcast-sync", "client_secret": "sync-secret"})
                r = await c.get(f"{self.fakes.base}/kc/admin/realms/inkcast/events",
                                params={"type": "DELETE_ACCOUNT", "user": sub, "max": "1"},
                                headers={"Authorization": f"Bearer {r.json()['access_token']}"})
                return [e["userId"] for e in r.json()]

        async def tick(sync):
            async with httpx2.AsyncClient(timeout=10) as http:
                sync.http = http
                return await sync.tick()

        sync = KeycloakSync(self.config, self.app.store, None, self.app.metrics)
        self.assertEqual(asyncio.run(tick(sync))["erased"], 0, "the cursors are set")
        self.fakes.kc_account_console_delete("kc-ead-carol")
        self.fakes.kc_admin_change("kc-ead-dave", delete=True)
        self.assertEqual(asyncio.run(events_for("kc-ead-carol")), ["kc-ead-carol"])
        self.assertEqual(asyncio.run(events_for("kc-ead-dave")), [], "the fake filters by user, as Keycloak does")

        done = asyncio.run(tick(sync))
        self.assertEqual((done["disabled"], done["erased"]), (1, 1))
        self.assertIsNone(identity("kc-ead-carol"), "carol: erased on her event")
        self.assertIsNotNone(identity("kc-ead-dave"), "dave: cut off, his rows kept for the grace period")
        self.assertEqual((asyncio.run(me("kc-ead-carol")), asyncio.run(me("kc-ead-dave"))), (401, 401))
        self.assertIn('inkcast_identity_users_deleted_total{via="event"} 1', self.app.metrics.render())
        before = len(self.app.store.user_erasures())

        sync.last_reconcile = 0
        self.assertEqual(asyncio.run(tick(sync))["erased"], 0, "the reconcile finds dave gone, inside the grace period")
        sync.clock = lambda: time.time() + self.config.keycloak_erase_after_days * DAY
        sync.last_reconcile = 0
        self.assertEqual(asyncio.run(tick(sync))["erased"], 1)
        self.assertIsNone(identity("kc-ead-dave"), "dave: erased 30 days after the cut-off")
        self.assertIn('inkcast_identity_users_deleted_total{via="reconcile"} 1', self.app.metrics.render())
        self.assertEqual(len(self.app.store.user_erasures()), before + 1, "one audit record")
        self.assertEqual([r["user_hash"] for r in self.app.store.user_erasures()][-1],
                         user_hash(self.app.store._erasure_salt, dave["id"]))
        sync.last_reconcile = 0
        self.assertEqual(asyncio.run(tick(sync))["erased"], 0, "nothing left to erase; nothing counted again")
        self.assertIn('inkcast_identity_users_deleted_total{via="reconcile"} 1', self.app.metrics.render())


if __name__ == "__main__":
    unittest.main()
