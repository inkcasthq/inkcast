# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #139: the Keycloak sync's breaker counts in the admin-events pass too.

A scripted mass deletion or disabling through Keycloak's admin API (a bad script, a compromised administrator account)
arrives as USER admin events, one person at a time, and the events pass (every 30 s) acted on each as it arrived:
cut off, their erasure clock running (erased at the next reconcile with INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=0). The
reconcile's breaker never saw the burst: a person already cut off is not newly gone to it. Now the events pass reads
everyone its events name before it acts on anyone, and counts the people newly found gone or disabled, together with
those the admin-events passes cut off within the last INKCAST_KEYCLOAK_RECONCILE_INTERVAL (a script that spreads its
calls over several passes is one burst), against the people Inkcast knows from this Keycloak, at the reconcile's
thresholds (INKCAST_KEYCLOAK_BREAKER_MIN and _PERCENT). At them it holds this pass's people as a trip of its own: the
same record, confirmation and dismissal as a reconcile's. Below them one person's deletion is acted on at once, as
before. The harness (a mock Keycloak admin API, one database file, a clock the test moves, a personal access token per
person) is ``tests/test_sync_breaker_disabled.py``'s. On main the burst tests fail on their assertions: everyone is
cut off and no trip is recorded."""

import logging
import re
import unittest

from inkcast.libs.db import Store
from inkcast.libs.db.identity import BREAKER_DISABLED, BREAKER_GONE
from test_sync_breaker import KEY, PREFIX, T0
from test_sync_breaker_disabled import Held


def _event(at: float, keycloak_id: str, operation: str) -> dict:
    """A USER admin event as Keycloak stores it (milliseconds); ``operation`` is DELETE or UPDATE."""
    return {"time": int(at * 1000), "resourceType": "USER", "operationType": operation, "resourcePath": f"users/{keycloak_id}"}


class Events(Held):
    def started(self, days: "int | None" = 30, **breaker):
        """A worker whose cursors are set by a first, empty pass, so the events that follow are read."""
        worker = self.sync(days, **breaker)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        return worker

    def deleted(self, people: list[dict], at: "float | None" = None) -> None:
        """An administrator (or a script with their token) deletes these people: gone, with a DELETE event each."""
        at = self.now + 1 if at is None else at
        self.kc.gone.update(u["kc"] for u in people)
        self.kc.admin_events.extend(_event(at, u["kc"], "DELETE") for u in people)

    def disabled(self, people: list[dict], at: "float | None" = None) -> None:
        at = self.now + 1 if at is None else at
        self.kc.disabled.update(u["kc"] for u in people)
        self.kc.admin_events.extend(_event(at, u["kc"], "UPDATE") for u in people)

    def tripped(self, records: list) -> logging.LogRecord:
        (line,) = [r for r in records if getattr(r, "event", "") == "identity.sync.breaker.tripped"]
        return line


class AMassDeletionInOnePass(Events):
    def test_twenty_user_delete_events_hold_everyone_and_record_a_trip_in_every_mode(self):
        for days in (30, 0, None):
            with self.subTest(days=days):
                self.setUp()
                people = self.people(20)
                worker = self.started(days)
                self.deleted(people)
                with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
                    done = self.tick(worker, T0 + 2)
                self.assertEqual(done, {"checked": 20, "disabled": 0, "erased": 0}, "read, held: nobody cut off")
                for user in people:
                    self.assert_untouched(user)
                    self.assert_working(user)
                (trip,) = self.trips()
                self.assertEqual((trip["status"], trip["gone"], trip["disabled"], trip["read"], trip["tripped_at"]),
                                 ("pending", 20, 0, 20, T0 + 2), "a trip of its own, counted against the people known")
                self.assertEqual(sorted((p["user_id"], p["kind"]) for p in self.store.breaker_trip_people(trip["id"])),
                                 sorted((u["id"], BREAKER_GONE) for u in people), "exactly the people found gone")
                self.assertEqual(self.gauge(), 1, "inkcast_identity_sync_breaker_open 1")
                line = self.tripped(logs.records)
                self.assertEqual((line.levelno, line.pass_id, line.gone, line.disabled, line.read, line.via),
                                 (logging.CRITICAL, trip["id"], 20, 0, 20, "event"))
                self.assertNotRegex(line.getMessage(), rf"\b{re.escape(people[0]['kc'])}\b", "no Keycloak id in the log")
                self.assertNotIn(people[0]["id"], line.getMessage(), "no user id in the log line either")
                self.assertEqual(self.counted("inkcast_identity_users_disabled_total"), {}, "nobody counted as cut off")
                self.assertEqual(worker.last_success, T0 + 2, "a pass that tripped is a pass that succeeded")
                # The look-back reads the events again: still held; and the reconcile, with the breaker open, holds
                # them too, erasing nobody even with 0 days.
                self.assertEqual(self.tick(worker, T0 + 32)["disabled"], 0)
                done = self.reconcile(worker, T0 + 600)
                self.assertEqual((done["disabled"], done["erased"]), (0, 0), "held: no grace period runs")
                for user in people:
                    self.assert_untouched(user)
                self.assertEqual(len(self.trips()), 1, "one trip, however many passes")

    def test_confirming_the_pass_cuts_them_off_and_the_reconcile_erases_them_as_the_policy_says(self):
        people = self.people(20)
        worker = self.started(0)
        self.deleted(people)
        self.tick(worker, T0 + 2)
        (trip,) = self.trips()
        self.now = T0 + 100
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.assertEqual(self.operator("confirm-deletions", "--pass", trip["id"], "--by", "on-call")[0], 0)
        for user in people:
            self.assert_cut_off(user, deleted_at=T0 + 100)            # the clock runs from the confirmation
        confirmed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"][0]
        self.assertEqual((confirmed.applied, confirmed.gone, confirmed.skipped), (20, 20, 0))
        self.assertEqual(self.gauge(), 0, "closed")
        done = self.reconcile(worker, T0 + 600)
        self.assertEqual((done["disabled"], done["erased"], len(self.trips())), (0, 20, 1), "0 days: erased, no new trip")
        for user in people:
            self.assert_erased(user)


class AMassDisablingInOnePass(Events):
    def test_twenty_user_update_events_disabling_everyone_hold_them_and_a_dismissal_releases_them(self):
        people = self.people(20)
        worker = self.started(30)
        self.disabled(people)
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.tick(worker, T0 + 2)
        self.assertEqual(done, {"checked": 20, "disabled": 0, "erased": 0})
        for user in people:
            self.assert_held(user)                                   # OAuth cut, PAT refused, not revoked
            self.assertEqual(self.me(user)[1].get("error"), "token_held")
        (trip,) = self.trips()
        self.assertEqual((trip["gone"], trip["disabled"], trip["read"]), (0, 20, 20))
        self.assertEqual({p["kind"] for p in self.store.breaker_trip_people(trip["id"])}, {BREAKER_DISABLED})
        self.assertEqual(len(self.store.breaker_trip_people(trip["id"])), 20)
        self.assertEqual(sorted((r.user_id, r.via) for r in logs.records if getattr(r, "event", "") == "identity.user.held"),
                         sorted((u["id"], "event") for u in people), "held by the events pass, one line each")
        self.assertEqual(self.tripped(logs.records).disabled, 20)
        self.kc.disabled.clear()                                     # the script is stopped and undone
        self.now = T0 + 100
        self.assertEqual(self.operator("dismiss-deletions", "--pass", trip["id"], "--by", "on-call")[0], 0)
        for user in people:
            self.assert_working(user)
        self.assertEqual(self.gauge(), 0)
        done = self.reconcile(worker, T0 + 600)                     # the look-back reads the events again, then everyone
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (40, 0, 0), "enabled again: nothing to do")
        self.assertEqual(len(self.trips()), 1)

    def test_gone_and_disabled_in_one_pass_count_together_and_each_kind_is_held_by_its_rule(self):
        people = self.people(50)                                     # 3 + 3 of 50: 12 % > 10 %, 6 >= 5; neither alone
        gone, disabled = people[:3], people[3:6]
        worker = self.started(30)
        self.deleted(gone)
        self.disabled(disabled)
        done = self.tick(worker, T0 + 2)
        self.assertEqual((done["checked"], done["disabled"]), (6, 0))
        for user in gone:
            self.assert_untouched(user)
        for user in disabled:
            self.assert_held(user)
        (trip,) = self.trips()
        self.assertEqual((trip["gone"], trip["disabled"], trip["read"]), (3, 3, 50))
        self.assertEqual(sorted((p["user_id"], p["kind"]) for p in self.store.breaker_trip_people(trip["id"])),
                         sorted([(u["id"], BREAKER_GONE) for u in gone] + [(u["id"], BREAKER_DISABLED) for u in disabled]))


class ABurstSpreadOverPasses(Events):
    """A script that deletes five people every 30 s in a realm of a hundred: no single pass reaches the share
    (5 of 100 is 5 %), but the passes within one reconcile interval count together."""

    def test_twenty_deletions_over_four_passes_trip_at_the_pass_that_reaches_the_thresholds(self):
        people = self.people(100)
        batches = [people[i:i + 5] for i in range(0, 20, 5)]
        worker = self.started(0)
        for i, batch in enumerate(batches[:2]):                     # 5, then 5 + 5 = 10 of 100: not above 10 %
            at = T0 + 30 * (i + 1)
            self.deleted(batch, at + 1)
            self.assertEqual(self.tick(worker, at + 2)["disabled"], 5, f"pass {i + 1}: acted on at once, as before")
            for user in batch:
                self.assert_cut_off(user, deleted_at=at + 2)
        self.assertEqual(self.trips(), [], "10 of 100 is not above 10 %")
        at = T0 + 90
        self.deleted(batches[2], at + 1)                            # 5 + 10 = 15 of 100: 15 % > 10 %, 15 >= 5
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.tick(worker, at + 2)
        self.assertEqual((done["checked"], done["disabled"]), (15, 0), "its five held; the ten of the minute before, read again")
        for user in batches[2]:
            self.assert_untouched(user)
        (trip,) = self.trips()
        self.assertEqual((trip["gone"], trip["read"], trip["tripped_at"]), (5, 100, at + 2), "this pass's people, of everyone known")
        self.assertEqual(sorted(p["user_id"] for p in self.store.breaker_trip_people(trip["id"])), sorted(u["id"] for u in batches[2]))
        line = self.tripped(logs.records)
        self.assertEqual((line.gone, line.read, line.recent, line.via), (5, 100, 10, "event"), "the ten cut off before, counted")
        for user in batches[0] + batches[1]:
            self.assertIsNotNone(self.row(user)["disabled_at"], "cut off by the passes before the trip: stays cut off")
        at = T0 + 120
        self.deleted(batches[3], at + 1)                            # while the pass is held: held too, not in the pass
        self.assertEqual(self.tick(worker, at + 2)["disabled"], 0)
        for user in batches[3]:
            self.assert_untouched(user)
        self.assertEqual(len(self.store.breaker_trip_people(trip["id"])), 5, "not added to the pass")
        # The open breaker holds the erasure of the ten cut off before the trip too, 0 days or not; a confirmation
        # cuts off the pass's five, and the next reconcile erases everyone gone, as the policy says.
        done = self.reconcile(worker, T0 + 600)
        self.assertEqual((done["disabled"], done["erased"]), (0, 0), "held: no erasure while the pass waits")
        self.now = T0 + 700
        self.assertEqual(self.operator("confirm-deletions", "--pass", trip["id"], "--by", "on-call")[0], 0)
        for user in batches[2]:
            self.assert_cut_off(user, deleted_at=T0 + 700)
        done = self.reconcile(worker, T0 + 1200)
        # The last five, alone now, are cut off by this tick's events pass: a burst opens again, so whoever is gone
        # within its window (those five, the five confirmed at T0 + 700) waits for it to close (review of #418).
        self.assertEqual((done["disabled"], done["erased"]), (5, 10), "the ten cut off before the trip: erased")
        self.assertEqual(self.reconcile(worker, T0 + 1900)["erased"], 10, "the window closed: 0 days, all twenty erased")
        self.assertEqual(len(self.trips()), 1)

    def test_passes_further_apart_than_the_window_count_alone(self):
        people = self.people(100)
        worker = self.started(30)
        window = worker.config.keycloak_reconcile_seconds
        self.assertEqual(window, 600)
        for i in range(4):
            at = T0 + (window + 1) * (i + 1)
            worker.last_reconcile = at                              # the admin-events pass alone
            self.deleted(people[5 * i:5 * i + 5], at + 1)
            self.assertEqual(self.tick(worker, at + 2)["disabled"], 5, f"pass {i + 1}: five of a hundred, alone")
        self.assertEqual(self.trips(), [], "never five and more than 10 % within one window")
        self.assertEqual(self.counted("inkcast_identity_users_disabled_total"), {"deleted": 20})

    def test_a_trip_starts_the_count_afresh_so_a_dismissal_is_not_undone_by_the_next_deletion(self):
        people = self.people(100)
        worker = self.started(30)
        for i in range(2):
            at = T0 + 30 * (i + 1)
            self.deleted(people[5 * i:5 * i + 5], at + 1)
            self.assertEqual(self.tick(worker, at + 2)["disabled"], 5)
        self.deleted(people[10:15], T0 + 91)
        self.assertEqual(self.tick(worker, T0 + 92)["disabled"], 0, "15 of 100: tripped")
        (trip,) = self.trips()
        self.now = T0 + 100
        self.assertEqual(self.operator("dismiss-deletions", "--pass", trip["id"], "--by", "on-call")[0], 0)
        # One more deletion, within the window of the ten cut off before the trip. The look-back reads the dismissed
        # pass's five again too, still gone: 6 of 100 is below the share, so all six are cut off, as a reconcile would
        # (nothing was done to the gone, and "it trips again if it must"); counting the ten as well, 16 would trip.
        self.deleted([people[20]], T0 + 121)
        self.assertEqual(self.tick(worker, T0 + 122)["disabled"], 6, "below the thresholds: the trip judged the ten")
        self.assert_cut_off(people[20], deleted_at=T0 + 122)
        self.assertEqual(len(self.trips()), 1, "no second trip")


class BelowTheThresholds(Events):
    def test_one_deletion_still_acts_at_once(self):
        people = self.people(20)
        ada = people[0]
        worker = self.started(30)
        self.deleted([ada])
        done = self.tick(worker, T0 + 2)
        self.assertEqual((done["checked"], done["disabled"]), (1, 1))
        self.assert_cut_off(ada, deleted_at=T0 + 2)
        self.assertEqual(self.trips(), [])
        self.assertEqual(self.gauge(), 0)
        self.assertEqual(self.counted("inkcast_identity_users_disabled_total"), {"deleted": 1})

    def test_one_disable_still_acts_at_once(self):
        people = self.people(20)
        bob = people[10]
        worker = self.started(30)
        self.disabled([bob])
        self.assertEqual(self.tick(worker, T0 + 2)["disabled"], 1)
        self.assert_cut_off_disabled(bob)
        self.assertEqual(self.trips(), [])

    def test_the_boundaries_are_the_reconciles_measured_against_the_people_known(self):
        for n, gone, trips in ((20, 4, False), (20, 5, True), (50, 5, False), (50, 6, True), (100, 10, False), (100, 11, True)):
            with self.subTest(people=n, gone=gone):
                self.setUp()
                people = self.people(n)
                worker = self.started(30)
                self.deleted(people[:gone])
                done = self.tick(worker, T0 + 2)
                self.assertEqual((done["checked"], done["disabled"]), (gone, 0 if trips else gone))
                self.assertEqual(len(self.trips()), 1 if trips else 0)

    def test_the_settings_move_the_thresholds(self):
        for breaker, n, gone, trips in (({"keycloak_breaker_min": 2}, 20, 3, True), ({"keycloak_breaker_min": 2}, 20, 1, False),
                                        ({"keycloak_breaker_percent": 50}, 20, 10, False), ({"keycloak_breaker_percent": 50}, 20, 11, True)):
            with self.subTest(breaker=breaker, people=n, gone=gone):
                self.setUp()
                people = self.people(n)
                worker = self.started(30, **breaker)
                self.deleted(people[:gone])
                self.assertEqual((self.tick(worker, T0 + 2)["disabled"], len(self.trips())), (0 if trips else gone, 1 if trips else 0))

    def test_a_person_already_cut_off_is_not_counted_again(self):
        """The look-back reads an event again for a minute: a person the pass before cut off is not news."""
        people = self.people(20)
        worker = self.started(30)
        self.deleted(people[:4])
        self.assertEqual(self.tick(worker, T0 + 2)["disabled"], 4, "under the minimum: cut off")
        self.assertEqual(self.tick(worker, T0 + 32)["disabled"], 0, "read again, nothing new")
        self.assertEqual(self.trips(), [])

    def test_off_cuts_everyone_off_at_once_and_records_nothing(self):
        people = self.people(20)
        worker = self.started(0, keycloak_breaker=False)
        self.deleted(people)
        done = self.tick(worker, T0 + 2)
        self.assertEqual((done["checked"], done["disabled"]), (20, 20))
        self.assertEqual((self.trips(), self.gauge()), ([], 0))
        self.assertEqual(self.store.events_cutoffs_since(0), 0, "nothing counted: nothing reads it")
        self.assertEqual(self.reconcile(worker, T0 + 600)["erased"], 20, "0 days: erased at the next reconcile, as before")


class TheStore(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", KEY)
        self.addCleanup(self.store.close)

    def test_the_people_known_from_one_keycloak_are_counted_disabled_included(self):
        for name in ("ada", "bob", "cy"):
            self.store.upsert_user(PREFIX + name, name, None, None)
        other = self.store.upsert_user("oidc:https://other.example/realms/x#dan", "dan", None, None)
        self.assertEqual(self.store.count_users_with_identity_prefix(PREFIX), 3)
        ada = self.store.find_user_by_identity(PREFIX + "ada")
        self.store.disable_user(ada["id"])
        self.assertEqual(self.store.count_users_with_identity_prefix(PREFIX), 3, "the cut off are known too")
        self.assertEqual(self.store.count_users_with_identity_prefix("oidc:https://other.example/realms/x#"), 1)
        self.assertTrue(sum(self.store.erase_user(other["id"]).values()))
        self.assertEqual(self.store.count_users_with_identity_prefix("oidc:https://other.example/realms/x#"), 0)

    def test_the_window_sums_the_passes_cut_offs_and_forgets_the_old(self):
        self.assertEqual(self.store.events_cutoffs_since(0), 0)
        self.store.record_events_cutoffs(T0, 5, since=T0 - 600)
        self.store.record_events_cutoffs(T0 + 30, 3, since=T0 - 570)
        self.assertEqual(self.store.events_cutoffs_since(T0 - 570), 8)
        self.assertEqual(self.store.events_cutoffs_since(T0 + 1), 3, "only the passes since")
        self.store.record_events_cutoffs(T0 + 700, 1, since=T0 + 100)           # the first two are older than the window
        self.assertEqual(self.store.events_cutoffs_since(0), 1, "forgotten: never counted again, however far back")

    def test_a_trip_clears_the_window(self):
        ada = self.store.upsert_user(PREFIX + "ada", "ada", None, None)
        self.store.record_events_cutoffs(T0, 5, since=T0 - 600)
        self.store.record_breaker_trip(T0 + 30, 1, 10, [(ada["id"], ada["last_login_at"])])
        self.assertEqual(self.store.events_cutoffs_since(0), 0, "the trip judged them: the count starts afresh")


class ThePassesAreQuick(Events):
    """The events pass's count is one COUNT over the users and one SUM over the passes' rows, never a scan per
    person; the write it adds is one row per pass that cut anyone off, under the same lock as the cut-offs."""

    def test_a_burst_in_a_realm_of_thousands_trips_and_the_count_runs_once(self):
        people = self.people(2000)
        worker = self.started(30)
        self.deleted(people[:250])                                  # 250 of 2000: 12.5 % > 10 %
        statements: list[str] = []
        conn = self.store._conn
        conn.set_trace_callback(lambda sql: statements.append(sql) if "sync_events_cutoffs" in sql or "COUNT(*)" in sql else None)
        try:
            done = self.tick(worker, T0 + 2)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual((done["checked"], done["disabled"]), (250, 0))
        (trip,) = self.trips()
        self.assertEqual((trip["gone"], trip["read"]), (250, 2000))
        self.assertEqual(len([s for s in statements if "COUNT(*)" in s and "FROM users" in s]), 1, statements)
        self.assertEqual(len([s for s in statements if "SUM(people)" in s]), 1, statements)


if __name__ == "__main__":
    unittest.main()
