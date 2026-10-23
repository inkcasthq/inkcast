# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""E9.F9.5.S8 (issue #135): the circuit breaker holds a mass "disabled" too; sign-in is cut, personal access tokens held.

Keycloak reports the users of a switched-off *importing* LDAP provider as ``enabled: false``, not 404 (26.6.1,
``UserStorageManager.validateFederatedUser``). The breaker of #105 counted 404s only, so switching LDAP off cut everyone
in it off at once and revoked their personal access tokens for good; signing in again did not restore them. Now a
reconcile counts the people newly disabled with the people newly gone, against the same thresholds. Above them the
disabled are **held**: their OAuth tokens, codes and pending consents go at once (reversible: a sign-in restores
them, and Keycloak refuses that while they are disabled), their personal access tokens are refused but not revoked,
and the pass records them with their kind. Confirming the pass revokes those tokens; dismissing lifts the hold and
they work again; Keycloak reporting a held person enabled again lifts it too. Below the thresholds, and with the
breaker off, a disabled person is cut off at once as before.

Module-level imports name only what main has, so on main these tests fail on their assertions (every PAT revoked at
once, no trip) rather than on an import."""

import asyncio
import logging
import os
import sqlite3
import tempfile
import unittest

import httpx2
from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.db import Store
from inkcast.libs.http.app import compose
from test_sync_breaker import KEY, MODES, PREFIX, T0, Scenario

DAY = 86400


class Held(Scenario):
    """The PR's harness, plus a personal access token per person and the REST API over the same database file."""

    def setUp(self):
        super().setUp()
        self.pats: dict[str, str] = {}
        self.api: "TestClient | None" = None

    def people(self, n: int, prefix: str = "p") -> list[dict]:
        rows = super().people(n, prefix)
        for user in rows:
            self.pats[user["id"]] = self.store.create_api_token(user["id"], "ci", ["posts:read"], None)[0]
        return rows

    def pat_works(self, user: dict) -> bool:
        return self.store.find_api_token(self.pats[user["id"]]) is not None

    def pat_revoked(self, user: dict) -> bool:
        return self.store.list_api_tokens(user["id"]) == []

    def me(self, user: dict) -> tuple[int, dict]:
        """``GET /api/v1/me`` with the person's PAT, through the REST API's bearer check: (status, body)."""
        if self.api is None:
            specs = resolve("PublishingApi")
            self.api = TestClient(compose(specs, build(self.config(30), specs)), base_url="http://localhost:8787")
            self.addCleanup(self.api.close)
        r = self.api.get("/api/v1/me", headers={"Authorization": f"Bearer {self.pats[user['id']]}"})
        return r.status_code, r.json()

    def assert_held(self, user: dict) -> None:
        row = self.row(user)
        self.assertIsNotNone(row, f"{user['kc']} keeps their row")
        self.assertIsNone(row["disabled_at"], f"{user['kc']} is not cut off")
        self.assertIsNone(row["deleted_at"], f"{user['kc']}: no grace period runs")
        self.assertIsNotNone(row.get("held_at"), f"{user['kc']} is held")
        self.assertIsNone(self.store.get_token(f"ic_at_{user['kc']:x<43}", "access"), f"{user['kc']}'s OAuth token is cut")
        self.assertFalse(self.pat_revoked(user), f"{user['kc']}'s PAT is not revoked")
        self.assertFalse(self.pat_works(user), f"{user['kc']}'s PAT is refused while held")

    def assert_cut_off_disabled(self, user: dict) -> None:
        """Cut off for being disabled: tokens and PATs gone, no grace period (Keycloak has them)."""
        self.assert_cut_off(user)
        self.assertIsNone(self.row(user)["deleted_at"], f"{user['kc']}: present, so no grace period")
        self.assertIsNone(self.row(user).get("held_at"), f"{user['kc']} is not held any more")
        self.assertTrue(self.pat_revoked(user), f"{user['kc']}'s PAT is revoked")

    def assert_working(self, user: dict) -> None:
        """Neither cut off nor held, and the PAT works. The OAuth token is not asserted: a hold cuts it, and only a
        sign-in mints a new one (``assert_untouched`` is for a person nothing happened to)."""
        row = self.row(user)
        self.assertIsNotNone(row, f"{user['kc']} keeps their row")
        self.assertIsNone(row["disabled_at"], f"{user['kc']} is not cut off")
        self.assertIsNone(row["deleted_at"], f"{user['kc']}: no grace period runs")
        self.assertIsNone(row.get("held_at"), f"{user['kc']} is not held")
        self.assertTrue(self.pat_works(user), f"{user['kc']}'s PAT works")


class AMassDisabled(Held):
    def test_twenty_newly_disabled_are_held_their_oauth_cut_their_pats_refused_not_revoked_and_a_trip_recorded(self):
        for mode, days in MODES.items():
            with self.subTest(mode=mode):
                self.setUp()
                people = self.people(20)
                self.kc.disabled.update(u["kc"] for u in people)        # the importing LDAP provider was switched off
                worker = self.sync(days)
                with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
                    done = self.reconcile(worker)
                self.assertEqual(done, {"checked": 20, "disabled": 0, "erased": 0}, "nobody is cut off")
                for user in people:
                    self.assert_held(user)
                    status, body = self.me(user)
                    self.assertEqual((status, body["error"]), (401, "token_held"), f"{user['kc']}: refused, and told why")
                    self.assertIn("operator", body["message"])
                self.assertEqual(self.counted("inkcast_identity_users_disabled_total"), {}, "a hold is not a disable")
                trips = self.trips()
                self.assertEqual(len(trips), 1, "one trip record")
                trip = trips[0]
                self.assertEqual((trip["status"], trip["gone"], trip["disabled"], trip["read"], trip["tripped_at"]),
                                 ("pending", 0, 20, 20, T0))
                held = self.store.breaker_trip_people(trip["id"])
                self.assertEqual(sorted(p["user_id"] for p in held), sorted(u["id"] for u in people))
                self.assertEqual({p["kind"] for p in held}, {"disabled"}, "recorded with their kind")
                self.assertEqual(self.gauge(), 1, "inkcast_identity_sync_breaker_open 1")
                tripped = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.tripped"]
                self.assertEqual(len(tripped), 1)
                self.assertEqual((tripped[0].pass_id, tripped[0].gone, tripped[0].disabled, tripped[0].read),
                                 (trip["id"], 0, 20, 20))
                self.assertEqual(tripped[0].levelno, logging.CRITICAL)
                self.assertNotIn(people[0]["kc"], tripped[0].getMessage(), "no Keycloak id in the log")
                self.assertNotIn(people[0]["id"], tripped[0].getMessage())
                held_lines = [r for r in logs.records if getattr(r, "event", "") == "identity.user.held"]
                self.assertEqual(sorted(r.user_id for r in held_lines), sorted(u["id"] for u in people), "one line each")
                self.assertTrue(all(r.revoked_oauth == 1 and r.via == "reconcile" for r in held_lines))
                self.assertFalse(any(people[0]["kc"] in r.getMessage() for r in held_lines), "no Keycloak id")
                # Still open at the next reconcile: still held, no PAT revoked, no second trip.
                with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
                    self.assertEqual(self.reconcile(worker, T0 + 600), {"checked": 20, "disabled": 0, "erased": 0})
                for user in people:
                    self.assert_held(user)
                self.assertEqual(len(self.trips()), 1)
                open_lines = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.held"]
                self.assertEqual([(r.pass_id, r.held, r.disabled) for r in open_lines], [(trip["id"], 0, 20)])
                self.assertEqual(self.store.user_erasures(), [], "no erasure, not even with 0 days")

    def test_the_boundaries_are_the_404_breakers(self):
        for n, disabled, trips in ((10, 4, False), (10, 5, True), (50, 5, False), (50, 6, True), (100, 10, False), (100, 11, True)):
            with self.subTest(people=n, disabled=disabled):
                self.setUp()
                people = self.people(n)
                self.kc.disabled.update(u["kc"] for u in people[:disabled])
                done = self.reconcile(self.sync(30))
                self.assertEqual(done["disabled"], 0 if trips else disabled)
                self.assertEqual(len(self.trips()), 1 if trips else 0)
                for user in people[:disabled]:
                    if trips:
                        self.assert_held(user)
                    else:
                        self.assert_cut_off_disabled(user)
                for user in people[disabled:]:
                    self.assert_untouched(user)
                    self.assert_working(user)

    def test_a_person_already_cut_off_is_not_newly_disabled(self):
        """The count is of people newly found disabled: one cut off earlier (an administrator disabled them, alone) is
        not news, however often the reconcile sees them disabled; nor is one the events pass cut off."""
        people = self.people(20)
        worker = self.sync(30)
        self.kc.disabled.add(people[0]["kc"])
        self.assertEqual(self.reconcile(worker)["disabled"], 1)
        self.assert_cut_off_disabled(people[0])
        self.kc.disabled.update(u["kc"] for u in people[1:5])            # four more: 4 newly, under the minimum
        self.assertEqual(self.reconcile(worker, T0 + 600)["disabled"], 4)
        self.assertEqual(self.trips(), [])
        self.kc.disabled.update(u["kc"] for u in people[5:10])           # five more at once: trips, counting these five
        self.assertEqual(self.reconcile(worker, T0 + 1200)["disabled"], 0)
        self.assertEqual((self.trips()[0]["disabled"], self.trips()[0]["gone"]), (5, 0))
        self.assertEqual(len(self.store.breaker_trip_people(self.trips()[0]["id"])), 5)


class Resolution(Held):
    def test_confirming_revokes_the_held_pats_and_audits_it(self):
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people)
        worker = self.sync(30)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        self.now = T0 + 3600
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            status, out = self.operator("confirm-deletions", "--pass", pass_id, "--by", "lateef")
        self.assertEqual(status, 0, out)
        for user in people:
            self.assert_cut_off_disabled(user)
            self.assertEqual(self.me(user)[0], 401)
        trip = self.store.get_breaker_trip(pass_id)
        self.assertEqual((trip["status"], trip["resolved_at"], trip["resolved_by"]), ("confirmed", T0 + 3600, "lateef"))
        confirmed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"]
        self.assertEqual(len(confirmed), 1, "one audit line")
        self.assertEqual((confirmed[0].pass_id, confirmed[0].applied, confirmed[0].skipped, confirmed[0].by),
                         (pass_id, 20, 0, "lateef"))
        self.assertEqual((confirmed[0].gone, confirmed[0].disabled), (0, 20))
        cut = [r for r in logs.records if getattr(r, "event", "") == "identity.user.disabled"]
        self.assertEqual(sorted(r.user_id for r in cut), sorted(u["id"] for u in people))
        self.assertEqual({(r.reason, r.via, r.revoked_pats, r.revoked_oauth) for r in cut}, {("disabled", "confirm", 1, 0)},
                         "the PAT now; the OAuth token went when they were held")
        self.assertEqual(self.gauge(), 0, "closed")
        self.assertEqual(self.store.breaker_trip_people(pass_id), [], "a resolved pass keeps no ids")
        # Closed: the next reconcile finds them cut off already, and nothing newly disabled.
        self.assertEqual(self.reconcile(worker, T0 + 3600 + 600), {"checked": 20, "disabled": 0, "erased": 0})
        self.assertEqual(len(self.trips()), 1, "no new trip")
        for user in people:
            self.assert_cut_off_disabled(user)
        self.assertEqual(self.reconcile(worker, T0 + 400 * DAY)["erased"], 0, "present, disabled: never erased")

    def test_dismissing_makes_the_pats_work_again(self):
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people)
        worker = self.sync(0)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        for user in people:
            self.assertEqual(self.me(user)[0], 401)
        self.now = T0 + 60
        with self.assertLogs("inkcast.identity.sync", level="INFO") as logs:
            status, out = self.operator("dismiss-deletions", "--pass", pass_id, "--by", "lateef")
        self.assertEqual(status, 0, out)
        for user in people:
            row = self.row(user)
            self.assertIsNone(row.get("held_at"), f"{user['kc']} is released")
            self.assertIsNone(row["disabled_at"])
            self.assertTrue(self.pat_works(user), f"{user['kc']}'s PAT works again")
            self.assertEqual(self.me(user)[0], 200)
            self.assertIsNone(self.store.get_token(f"ic_at_{user['kc']:x<43}", "access"), "the OAuth token stays cut: a sign-in restores it")
        trip = self.store.get_breaker_trip(pass_id)
        self.assertEqual((trip["status"], trip["resolved_at"], trip["resolved_by"]), ("dismissed", T0 + 60, "lateef"))
        dismissed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.dismissed"]
        self.assertEqual([(r.pass_id, r.by, r.released) for r in dismissed], [(pass_id, "lateef", 20)])
        released = [r for r in logs.records if getattr(r, "event", "") == "identity.user.released"]
        self.assertEqual(sorted(r.user_id for r in released), sorted(u["id"] for u in people))
        self.assertEqual({r.via for r in released}, {"dismiss"})
        self.assertEqual(self.gauge(), 0)
        self.kc.disabled.clear()                                         # LDAP is back: everyone enabled
        self.assertEqual(self.reconcile(worker, T0 + 600), {"checked": 20, "disabled": 0, "erased": 0})
        self.assertEqual(len(self.trips()), 1)
        for user in people:
            self.assert_working(user)
        self.kc.disabled.update(u["kc"] for u in people)                 # not fixed after all: it trips again, a new pass
        self.reconcile(worker, T0 + 1200)
        for user in people:
            self.assert_held(user)
        self.assertEqual([t["status"] for t in self.trips()], ["dismissed", "pending"])
        self.assertNotEqual(self.trips()[1]["id"], pass_id)

    def test_trips_lists_both_counts(self):
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people[:3])
        self.kc.gone.update(u["kc"] for u in people[3:6])
        self.reconcile(self.sync(30))
        status, out = self.operator("trips")
        self.assertEqual(status, 0)
        self.assertIn("3 of 20 gone, 3 disabled", out)
        self.assertIn("pending", out)
        for user in people[:6]:
            self.assertNotIn(user["id"], out)


class OnePerson(Held):
    def test_one_person_disabled_is_cut_off_at_once_as_today(self):
        people = self.people(20)
        ada = people[0]
        self.kc.disabled.add(ada["kc"])                                  # an administrator disabled her
        worker = self.sync(30)
        done = self.reconcile(worker)
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (20, 1, 0))
        self.assert_cut_off_disabled(ada)
        status, body = self.me(ada)
        self.assertEqual((status, body["error"]), (401, "invalid_token"), "revoked, not held")
        for user in people[1:]:
            self.assert_untouched(user)
            self.assert_working(user)
        self.assertEqual(self.trips(), [])
        self.assertEqual(self.gauge(), 0)
        self.assertEqual(self.counted("inkcast_identity_users_disabled_total"), {"disabled": 1})
        self.kc.disabled.discard(ada["kc"])                              # enabled again: her PAT stays revoked
        self.reconcile(worker, T0 + 600)
        self.assertTrue(self.pat_revoked(ada))
        self.assertIsNotNone(self.row(ada)["disabled_at"], "cut off until she signs in")

    def test_the_events_pass_cuts_a_disabled_person_off_at_once_while_the_breaker_is_closed(self):
        people = self.people(20)
        bob = people[10]
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.kc.disabled.add(bob["kc"])
        self.kc.admin_events.append({"time": int(self.now * 1000) + 1000, "resourceType": "USER", "operationType": "UPDATE",
                                     "resourcePath": f"users/{bob['kc']}"})
        done = self.tick(worker, T0 + 2)
        self.assertEqual((done["checked"], done["disabled"]), (1, 1))
        self.assert_cut_off_disabled(bob)
        self.assertEqual(self.trips(), [])


class AMixedTrip(Held):
    def test_gone_and_disabled_together_trip_and_each_kind_is_held_by_its_rule(self):
        """Three gone and three disabled of twenty: neither alone reaches the minimum of five; together six people
        newly lose access at once, 30 % of those read. The gone are held as #105 holds them (nothing done), the
        disabled as this story does (OAuth cut, PATs held)."""
        people = self.people(20)
        gone, disabled, present = people[:3], people[3:6], people[6:]
        self.kc.gone.update(u["kc"] for u in gone)
        self.kc.disabled.update(u["kc"] for u in disabled)
        worker = self.sync(0)
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.reconcile(worker)
        self.assertEqual(done, {"checked": 20, "disabled": 0, "erased": 0})
        for user in gone + present:
            self.assert_untouched(user)                                  # the gone: as #105 holds them, nothing done
            self.assert_working(user)
        for user in disabled:
            self.assert_held(user)
        trip = self.trips()[0]
        self.assertEqual((trip["gone"], trip["disabled"], trip["read"]), (3, 3, 20))
        held = {p["user_id"]: p["kind"] for p in self.store.breaker_trip_people(trip["id"])}
        self.assertEqual(held, {**{u["id"]: "gone" for u in gone}, **{u["id"]: "disabled" for u in disabled}})
        tripped = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.tripped"][0]
        self.assertEqual((tripped.gone, tripped.disabled, tripped.read), (3, 3, 20))
        # Confirming applies each kind's rule: the gone are cut off and marked gone from now (erased by the next
        # reconcile with 0 days); the disabled are cut off, their PATs revoked, and never erased.
        self.now = T0 + 3600
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.assertEqual(self.operator("confirm-deletions", "--pass", trip["id"])[0], 0)
        for user in gone:
            self.assert_cut_off(user, deleted_at=T0 + 3600)
            self.assertTrue(self.pat_revoked(user))
        for user in disabled:
            self.assert_cut_off_disabled(user)
        confirmed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"][0]
        self.assertEqual((confirmed.applied, confirmed.skipped, confirmed.gone, confirmed.disabled), (6, 0, 3, 3))
        cut = {r.user_id: r.reason for r in logs.records if getattr(r, "event", "") == "identity.user.disabled"}
        self.assertEqual(cut, {**{u["id"]: "deleted" for u in gone}, **{u["id"]: "disabled" for u in disabled}})
        done = self.reconcile(worker, T0 + 3600 + 600)
        self.assertEqual((done["disabled"], done["erased"]), (0, 3), "0 days: the three gone are erased; the disabled stay")
        for user in gone:
            self.assert_erased(user)
        for user in disabled:
            self.assert_cut_off_disabled(user)

    def test_dismissing_a_mixed_trip_lifts_the_holds_and_does_nothing_to_the_gone(self):
        people = self.people(20)
        gone, disabled = people[:3], people[3:6]
        self.kc.gone.update(u["kc"] for u in gone)
        self.kc.disabled.update(u["kc"] for u in disabled)
        worker = self.sync(30)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.assertEqual(self.operator("dismiss-deletions", "--pass", pass_id)[0], 0)
        for user in gone:
            self.assert_untouched(user)
            self.assert_working(user)
        for user in disabled:
            self.assertTrue(self.pat_works(user))
            self.assertIsNone(self.row(user).get("held_at"))
        dismissed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.dismissed"][0]
        self.assertEqual(dismissed.released, 3)
        self.kc.gone.clear()
        self.kc.disabled.clear()
        self.assertEqual(self.reconcile(worker, T0 + 600), {"checked": 20, "disabled": 0, "erased": 0})
        for user in people:
            self.assert_working(user)

    def test_a_gone_trip_holds_a_person_disabled_meanwhile_and_a_disabled_trip_holds_a_404(self):
        """The breaker is open: whatever Keycloak says next about a person is held, by its kind, until an operator
        decides, and neither joins the pass. After a dismissal each is judged afresh by the next reconcile."""
        people = self.people(20)
        self.kc.gone.update(u["kc"] for u in people[:5])                 # a 404 trip
        worker = self.sync(30)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        later_disabled, later_gone = people[10], people[11]
        self.kc.disabled.add(later_disabled["kc"])
        self.kc.gone.add(later_gone["kc"])
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.reconcile(worker, T0 + 600)
        self.assertEqual((done["disabled"], done["erased"]), (0, 0))
        self.assert_held(later_disabled)
        self.assert_untouched(later_gone)
        self.assert_working(later_gone)
        self.assertEqual(len(self.store.breaker_trip_people(pass_id)), 5, "the pass keeps the people it found")
        open_lines = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.held"]
        self.assertEqual([(r.held, r.disabled) for r in open_lines], [(6, 1)])
        self.assertEqual(self.operator("dismiss-deletions", "--pass", pass_id)[0], 0)
        self.assert_held(later_disabled)                                 # not in the pass: still held until read again
        self.kc.gone.clear()                                             # Keycloak fixed for the five; the two are real
        self.kc.gone.add(later_gone["kc"])
        done = self.reconcile(worker, T0 + 1200)
        self.assertEqual(done["disabled"], 2, "closed: each is one person, acted on as the policy says")
        self.assert_cut_off_disabled(later_disabled)
        self.assert_cut_off(later_gone, deleted_at=T0 + 1200)
        self.assertEqual(len(self.trips()), 1)


class TheEventsPassWhileOpen(Held):
    def test_the_events_pass_holds_a_disabled_person_while_a_pass_is_held(self):
        """An administrator disables bob (a USER admin event) while the breaker is open: the events pass holds him
        rather than cutting him off (Keycloak refuses his sign-in anyway; only the PAT revocation waits). He is not
        in the pass, so a confirmation does not touch him: the next reconcile judges him afresh."""
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people[:5])
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        bob = people[10]
        self.kc.disabled.add(bob["kc"])
        self.kc.admin_events.append({"time": int(self.now * 1000) + 1000, "resourceType": "USER", "operationType": "UPDATE",
                                     "resourcePath": f"users/{bob['kc']}"})
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.tick(worker, T0 + 2)
        self.assertEqual((done["checked"], done["disabled"]), (1, 0), "read, held")
        self.assert_held(bob)
        self.assertEqual([(r.user_id, r.via) for r in logs.records if getattr(r, "event", "") == "identity.user.held"],
                         [(bob["id"], "event")])
        self.assertEqual(self.tick(worker, T0 + 32)["disabled"], 0, "the look-back reads his event again: still held")
        self.assert_held(bob)
        self.assertEqual(len(self.store.breaker_trip_people(pass_id)), 5, "not in the pass")
        self.now = T0 + 100
        self.assertEqual(self.operator("confirm-deletions", "--pass", pass_id)[0], 0)
        for user in people[:5]:
            self.assert_cut_off_disabled(user)
        self.assert_held(bob)
        done = self.reconcile(worker, T0 + 600)
        self.assertEqual(done["disabled"], 1, "closed: one person newly disabled, cut off as the policy says")
        self.assert_cut_off_disabled(bob)
        self.assertEqual(len(self.trips()), 1)

    def test_the_events_pass_releases_a_held_person_keycloak_enabled_again(self):
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people)
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        back = people[3]
        self.kc.disabled.discard(back["kc"])                             # an administrator enabled her again
        self.kc.admin_events.append({"time": int(self.now * 1000) + 1000, "resourceType": "USER", "operationType": "UPDATE",
                                     "resourcePath": f"users/{back['kc']}"})
        with self.assertLogs("inkcast.identity.sync", level="INFO") as logs:
            self.assertEqual(self.tick(worker, T0 + 2)["disabled"], 0)
        self.assertTrue(self.pat_works(back), "her PAT works again at once")
        self.assertIsNone(self.row(back).get("held_at"))
        self.assertEqual(self.me(back)[0], 200)
        self.assertEqual([(r.user_id, r.via) for r in logs.records if getattr(r, "event", "") == "identity.user.released"],
                         [(back["id"], "event")])
        self.now = T0 + 100
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.assertEqual(self.operator("confirm-deletions", "--pass", pass_id)[0], 0)
        self.assertTrue(self.pat_works(back), "she keeps her PAT: Keycloak has her enabled")
        self.assertIsNone(self.row(back)["disabled_at"])
        confirmed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"][0]
        self.assertEqual((confirmed.applied, confirmed.skipped), (19, 0), "she left the pass when read enabled")
        for user in people:
            if user is not back:
                self.assert_cut_off_disabled(user)


class ReEnabledBeforeConfirmation(Held):
    def test_a_held_person_the_reconcile_reads_enabled_again_keeps_their_pat_and_leaves_the_pass(self):
        """LDAP is back for two of the twenty before the operator acts (automation only: neither signs in). The
        reconcile while the breaker is open reads them enabled: their hold is lifted, their PATs work, and they leave
        the pass, so the confirmation revokes the other eighteen only."""
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people)
        worker = self.sync(30)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        back, still = people[:2], people[2:]
        for user in back:
            self.kc.disabled.discard(user["kc"])
        with self.assertLogs("inkcast.identity.sync", level="INFO") as logs:
            done = self.reconcile(worker, T0 + 600)
        self.assertEqual((done["disabled"], done["erased"]), (0, 0))
        for user in back:
            self.assertTrue(self.pat_works(user), f"{user['kc']}'s PAT works again")
            self.assertIsNone(self.row(user).get("held_at"))
            self.assertEqual(self.me(user)[0], 200)
        for user in still:
            self.assert_held(user)
        self.assertEqual(sorted(p["user_id"] for p in self.store.breaker_trip_people(pass_id)), sorted(u["id"] for u in still))
        released = [r for r in logs.records if getattr(r, "event", "") == "identity.user.released"]
        self.assertEqual(sorted((r.user_id, r.via) for r in released), sorted((u["id"], "reconcile") for u in back))
        self.now = T0 + 3600
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.assertEqual(self.operator("confirm-deletions", "--pass", pass_id, "--by", "lateef")[0], 0)
        for user in back:
            self.assertTrue(self.pat_works(user), f"{user['kc']} keeps their PAT")
            self.assert_working(user)
        for user in still:
            self.assert_cut_off_disabled(user)
        confirmed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"][0]
        self.assertEqual((confirmed.applied, confirmed.skipped, confirmed.disabled), (18, 0, 18))

    def test_a_held_person_who_signs_in_again_keeps_their_pat_and_is_skipped(self):
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people)
        worker = self.sync(30)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        back = people[0]
        self.kc.disabled.discard(back["kc"])                             # enabled again, and Keycloak lets her in
        self.now = T0 + 10
        self.assertEqual(self.store.upsert_user(PREFIX + back["kc"], back["kc"], None, None)["id"], back["id"])
        self.assertTrue(self.pat_works(back), "the sign-in lifts the hold")
        self.assertIsNone(self.row(back).get("held_at"))
        self.now = T0 + 3600
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.assertEqual(self.operator("confirm-deletions", "--pass", pass_id)[0], 0)
        self.assertTrue(self.pat_works(back))
        self.assert_working(back)
        confirmed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"][0]
        self.assertEqual((confirmed.applied, confirmed.skipped), (19, 1), "signed in since: spared")

    def test_a_person_who_signs_in_between_the_read_and_the_hold_is_not_held(self):
        """The reconcile reads everyone, then acts. Keycloak enabled one of them and let them in after their read:
        the sign-in is the one proof Inkcast has that Keycloak has them, so they are not held on the stale answer."""
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people)
        mock, back = self.kc, people[5]
        signed_in: list[str] = []

        def keycloak(request):
            if request.url.path.endswith(f"/users/{people[19]['kc']}") and not signed_in:   # the last read of the pass
                mock.disabled.discard(back["kc"])
                signed_in.append(self.store.upsert_user(PREFIX + back["kc"], back["kc"], None, None)["id"])
            return mock(request)

        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(keycloak))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))
        self.reconcile(self.sync(30))
        self.assertEqual(signed_in, [back["id"]])
        self.assertTrue(self.pat_works(back), "signed in after the read: not held")
        self.assertIsNotNone(self.store.get_token(f"ic_at_{back['kc']:x<43}", "access"), "nor her OAuth token cut")
        self.assertEqual(len(self.trips()), 1, "the other nineteen tripped the breaker all the same")
        for user in people:
            if user is not back:
                self.assert_held(user)


class BreakerOff(Held):
    def test_off_cuts_everyone_off_at_once_as_before_and_records_nothing(self):
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people)
        done = self.reconcile(self.sync(30, keycloak_breaker=False))
        self.assertEqual((done["disabled"], done["erased"]), (20, 0))
        for user in people:
            self.assert_cut_off_disabled(user)
        self.assertEqual(self.trips(), [])
        self.assertEqual(self.gauge(), 0)
        self.assertEqual(self.counted("inkcast_identity_users_disabled_total"), {"disabled": 20})

    def test_turned_off_with_a_pass_pending_the_reconcile_cuts_off_the_held_people(self):
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people)
        self.reconcile(self.sync(30))
        self.assertEqual((len(self.trips()), self.gauge()), (1, 1))
        for user in people:
            self.assert_held(user)
        done = self.reconcile(self.sync(30, keycloak_breaker=False), T0 + 600)
        self.assertEqual(done["disabled"], 20, "nothing is held: all twenty cut off, their PATs revoked")
        for user in people:
            self.assert_cut_off_disabled(user)
        self.assertEqual(self.gauge(), 0)


class TheStore(unittest.TestCase):
    """``hold_user`` cuts what a sign-in restores and nothing else; a token or code minted in the moment of the hold is
    refused, as after ``disable_user``; the held PAT is told apart from a revoked one for the refusal's message only."""

    def test_a_hold_cuts_oauth_and_codes_keeps_pats_refused_and_is_lifted_cleanly(self):
        store = Store(":memory:", KEY)
        self.addCleanup(store.close)
        ada = store.upsert_user(PREFIX + "ada", "ada", None, None)
        bob = store.upsert_user(PREFIX + "bob", "bob", None, None)
        store.save_token("at-ada", "access", "c1", ada["id"], ["inkcast"], None, "f1", 3600)
        store.save_token("rt-ada", "refresh", "c1", ada["id"], ["inkcast"], None, "f1", 3600)
        store.save_token("at-bob", "access", "c1", bob["id"], ["inkcast"], None, "f2", 3600)
        store.save_code("code-ada", "c1", ada["id"], "{}")
        pending = store.create_pending("c1", "{}")
        store.set_pending_user(pending, ada["id"])
        pat, _ = store.create_api_token(ada["id"], "ci", ["posts:read"], None)
        self.assertFalse(store.api_token_held(pat))

        self.assertEqual(store.hold_user(ada["id"], T0), {"oauth_tokens": 2, "codes": 1})
        self.assertEqual(store.get_user(ada["id"])["held_at"], T0)
        self.assertIsNone(store.get_token("at-ada", "access"))
        self.assertIsNone(store.get_code("code-ada"))
        self.assertIsNone(store.get_pending(pending), "a consent page open at the hold cannot be answered")
        self.assertIsNone(store.find_api_token(pat), "refused")
        self.assertTrue(store.api_token_held(pat), "but held, not revoked")
        self.assertEqual(len(store.list_api_tokens(ada["id"])), 1, "not revoked")
        self.assertIsNotNone(store.get_token("at-bob", "access"), "nobody else touched")
        self.assertIsNone(store.hold_user(ada["id"], T0 + 1), "held once")
        self.assertEqual(store.get_user(ada["id"])["held_at"], T0)
        # Minted in the moment of the hold: refused while it lasts.
        store.save_token("at-ada-late", "access", "c1", ada["id"], ["inkcast"], None, "f3", 3600)
        store.save_code("code-ada-late", "c1", ada["id"], "{}")
        self.assertIsNone(store.get_token("at-ada-late", "access"))
        self.assertIsNone(store.get_code("code-ada-late"))
        # Lifted: the PAT works; the tokens cut stay cut (a sign-in mints new ones).
        self.assertTrue(store.release_user(ada["id"]))
        self.assertFalse(store.release_user(ada["id"]), "released once")
        self.assertIsNone(store.get_user(ada["id"])["held_at"])
        self.assertIsNotNone(store.find_api_token(pat))
        self.assertFalse(store.api_token_held(pat))
        self.assertIsNone(store.get_token("at-ada", "access"))
        # Cut off while held: the hold ends with it, and the PAT is revoked for good.
        self.assertEqual(store.hold_user(ada["id"], T0 + 2), {"oauth_tokens": 1, "codes": 1})
        self.assertEqual(store.disable_user(ada["id"]), {"oauth_tokens": 0, "personal_access_tokens": 1, "codes": 0})
        self.assertIsNone(store.get_user(ada["id"])["held_at"])
        self.assertIsNone(store.find_api_token(pat))
        self.assertFalse(store.api_token_held(pat), "revoked: a plain refusal")
        self.assertIsNone(store.hold_user(ada["id"], T0 + 3), "a person cut off is not held")
        self.assertIsNone(store.hold_user("usr_nobody", T0), "nor an unknown one")

    def test_signing_in_lifts_the_hold(self):
        store = Store(":memory:", KEY)
        self.addCleanup(store.close)
        ada = store.upsert_user(PREFIX + "ada", "ada", None, None)
        pat, _ = store.create_api_token(ada["id"], "ci", ["posts:read"], None)
        store.hold_user(ada["id"], T0)
        self.assertIsNone(store.find_api_token(pat))
        back = store.upsert_user(PREFIX + "ada", "ada", None, None)
        self.assertEqual(back["id"], ada["id"])
        self.assertIsNone(back["held_at"])
        self.assertIsNone(store.get_user(ada["id"])["held_at"])
        self.assertIsNotNone(store.find_api_token(pat))

    def test_an_erased_person_leaves_the_pass_with_their_kind(self):
        store = Store(":memory:", KEY)
        self.addCleanup(store.close)
        ada, bob, carol = (store.upsert_user(PREFIX + n, n, None, None) for n in ("ada", "bob", "carol"))
        pass_id = store.record_breaker_trip(T0, 1, 10, [(ada["id"], ada["last_login_at"])],
                                            disabled=[(bob["id"], bob["last_login_at"]), (carol["id"], carol["last_login_at"])])
        self.assertEqual((store.get_breaker_trip(pass_id)["gone"], store.get_breaker_trip(pass_id)["disabled"]), (1, 2))
        self.assertEqual({p["user_id"]: p["kind"] for p in store.breaker_trip_people(pass_id)},
                         {ada["id"]: "gone", bob["id"]: "disabled", carol["id"]: "disabled"})
        self.assertEqual(store.erase_user(bob["id"])["sync_breaker_people"], 1)
        self.assertEqual(store.release_breaker_people([ada["id"], carol["id"]], "gone"), 1, "by kind: ada only")
        self.assertEqual([p["user_id"] for p in store.breaker_trip_people(pass_id)], [carol["id"]])
        self.assertEqual(store.release_breaker_people([carol["id"]]), 1, "any kind")
        self.assertEqual(store.breaker_trip_people(pass_id), [])


class Migration(unittest.TestCase):
    def test_an_existing_database_gains_the_kind_the_disabled_count_and_held_at(self):
        """A database from #131: trips with gone and read only, people without a kind, users without held_at. Its
        pending pass reads as a 404 pass (kind gone, 0 disabled) and resolves as before."""
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE users (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, display TEXT NOT NULL, email TEXT,
              name TEXT, created_at REAL NOT NULL, last_login_at REAL NOT NULL, disabled_at REAL, deleted_at REAL);
            INSERT INTO users VALUES ('usr_1', 'oidc:x#1', 'ada', NULL, NULL, 1, 1, NULL, NULL);
            CREATE TABLE sync_breaker_trips (id TEXT PRIMARY KEY, tripped_at REAL NOT NULL, gone INTEGER NOT NULL,
              read INTEGER NOT NULL, status TEXT NOT NULL, resolved_at REAL, resolved_by TEXT);
            INSERT INTO sync_breaker_trips VALUES ('pass_old', 5, 5, 20, 'pending', NULL, NULL);
            CREATE TABLE sync_breaker_people (pass_id TEXT NOT NULL, user_id TEXT NOT NULL, last_login_at REAL NOT NULL,
              PRIMARY KEY (pass_id, user_id));
            INSERT INTO sync_breaker_people VALUES ('pass_old', 'usr_1', 1);
        """)
        old.commit()
        old.close()
        store = Store(path, KEY)
        self.addCleanup(store.close)
        self.assertIsNone(store.get_user("usr_1")["held_at"])
        trip = store.get_breaker_trip("pass_old")
        self.assertEqual((trip["gone"], trip["disabled"], trip["read"], trip["status"]), (5, 0, 20, "pending"))
        self.assertEqual(store.breaker_trip_people("pass_old"), [{"user_id": "usr_1", "last_login_at": 1, "kind": "gone"}])
        self.assertIsNotNone(store.pending_breaker_trip())
        pat, _ = store.create_api_token("usr_1", "ci", ["posts:read"], None)
        self.assertIsNotNone(store.find_api_token(pat), "an old row is not held")
        Store(path, KEY).close()                           # opening again is a no-op, not an error


class TheAlert(unittest.TestCase):
    def test_the_alert_and_the_posture_check_say_disabled_too(self):
        from inkcast.libs.alerting import ALERTS
        from inkcast.libs.config import Config
        from inkcast.libs.secrets import Redactor, run_checks
        alert = {a.name: a for a in ALERTS}["InkcastIdentitySyncBreakerOpen"]
        self.assertIn("disabled", alert.description)
        self.assertIn("personal access tokens", alert.description)
        config = Config(public_url="https://inkcast.example.com", secret_key=KEY, database_path=":memory:",
                        environment="production", login="oidc", oidc_issuer="https://kc.example/realms/inkcast",
                        keycloak_sync_client_id="inkcast-sync")
        check = {c.id: c for c in run_checks(config, redactor=Redactor(), environ={})}["keycloak-breaker"]
        self.assertEqual(check.status, "pass")
        self.assertIn("disabled", check.detail)


if __name__ == "__main__":
    unittest.main()
