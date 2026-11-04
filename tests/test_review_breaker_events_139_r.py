# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #418 (#139, OB3): what the admin-events pass's breaker still lets a spread mass deletion erase.

The harness is ``tests/test_sync_breaker_events.py``'s: a mock Keycloak admin API, one database file, a clock the test
moves. In a realm of a hundred, a script deletes five people every 30 s; the third pass (15 of 100) trips."""

import unittest

from test_sync_breaker import T0
from test_sync_breaker_events import Events


class AReconcileBeforeTheTrip(Events):
    """With INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=0, the people the admin-events passes cut off below the thresholds were
    erased by a reconcile that ran before the pass that trips: the trip held no grace period for them, because they
    no longer existed. Their erasure waits until the window they were counted in has closed."""

    def test_the_people_a_spread_burst_cut_off_are_not_erased_by_a_reconcile_before_the_trip(self):
        people = self.people(100)
        batches = [people[i:i + 5] for i in range(0, 15, 5)]
        worker = self.started(0)
        for i, batch in enumerate(batches[:2]):                     # 5, then 10 of 100: acted on at once
            at = T0 + 30 * (i + 1)
            self.deleted(batch, at + 1)
            self.assertEqual(self.tick(worker, at + 2)["disabled"], 5)
        done = self.reconcile(worker, T0 + 95)                      # due between pass 2 and pass 3
        self.assertEqual(done["erased"], 0, "the burst they belong to is still open: their erasure waits")
        for user in batches[0] + batches[1]:
            self.assert_cut_off(user)
        self.deleted(batches[2], T0 + 121)
        self.assertEqual(self.tick(worker, T0 + 122)["disabled"], 0, "15 of 100: tripped")
        (trip,) = self.trips()
        self.assertEqual(self.reconcile(worker, T0 + 700)["erased"], 0, "the open breaker holds all fifteen")
        self.now = T0 + 800
        self.assertEqual(self.operator("confirm-deletions", "--pass", trip["id"], "--by", "on-call")[0], 0)
        self.assertEqual(self.reconcile(worker, T0 + 1400)["erased"], 15, "confirmed: 0 days erases every one")

    def test_the_same_tick_reconcile_does_not_erase_what_its_events_pass_just_cut_off(self):
        people = self.people(100)
        worker = self.started(0)
        self.deleted(people[:10], T0 + 31)
        worker.last_reconcile = 0                                   # the reconcile is due in this very tick
        done = self.tick(worker, T0 + 32)
        self.assertEqual((done["disabled"], done["erased"]), (10, 0), "cut off at once, not erased in the same breath")

    def test_one_deletion_is_still_cut_off_at_once_and_erased_once_its_window_has_closed(self):
        people = self.people(20)
        ada = people[0]
        worker = self.started(0)
        self.deleted([ada], T0 + 31)
        self.assertEqual(self.tick(worker, T0 + 32)["disabled"], 1, "acted on at once, as before")
        self.assert_cut_off(ada, deleted_at=T0 + 32)
        self.assertEqual(self.reconcile(worker, T0 + 100)["erased"], 0, "within the window: waits")
        self.assertEqual(self.reconcile(worker, T0 + 700)["erased"], 1, "the window closed: 0 days erases them")
        self.assert_erased(ada)

    def test_a_reconciles_own_cut_off_is_still_erased_at_once_with_0_days(self):
        people = self.people(20)
        worker = self.started(0)
        self.deleted(people[:1], T0 + 31)
        self.tick(worker, T0 + 32)                                  # one in the window, the burst open
        self.kc.gone.add(people[1]["kc"])                           # no event: only the reconcile finds this one
        done = self.reconcile(worker, T0 + 60)
        self.assertEqual((done["disabled"], done["erased"]), (1, 1), "the reconcile's own find: 0 is that reconcile")
        self.assert_erased(people[1])
        self.assert_cut_off(people[0])


class ASpreadBurstOfPeopleAlreadyDisabled(Events):
    """A person Keycloak had disabled (cut off, still in Keycloak) whom a script then deletes is newly gone, and one
    pass counts them so; but the window recorded only the people ``disable_user`` cut off now, never them, so the
    same deletions spread over passes never added up: twenty of a hundred deleted, no trip, all erased with 0 days."""

    def test_the_window_counts_the_newly_gone_who_were_already_disabled(self):
        people = self.people(100)
        for user in people[:20]:
            self.store.disable_user(user["id"])                     # disabled long ago: no deleted_at, no clock
        worker = self.started(0)
        for i in range(2):                                          # 5, then 10 of 100: acted on at once
            at = T0 + 30 * (i + 1)
            self.deleted(people[5 * i:5 * i + 5], at + 1)
            self.tick(worker, at + 2)
            for user in people[5 * i:5 * i + 5]:
                self.assertEqual(self.row(user)["deleted_at"], at + 2, "marked gone: the clock runs")
        self.assertEqual(self.store.events_cutoffs_since(0), 10, "both passes counted")
        self.deleted(people[10:15], T0 + 91)
        self.tick(worker, T0 + 92)
        (trip,) = self.trips()
        self.assertEqual((trip["gone"], trip["read"]), (5, 100), "15 of 100: the third pass trips, as for anyone")
        for user in people[10:15]:
            self.assertIsNone(self.row(user)["deleted_at"], "held: no clock")
        self.assertEqual(self.reconcile(worker, T0 + 600)["erased"], 0, "the open breaker erases nobody")

    def test_a_person_read_again_by_the_look_back_is_not_counted_twice(self):
        people = self.people(100)
        self.store.disable_user(people[0]["id"])
        worker = self.started(30)
        self.deleted(people[:1], T0 + 31)
        self.tick(worker, T0 + 32)
        self.tick(worker, T0 + 62)                                  # the look-back reads the event again
        self.assertEqual(self.store.events_cutoffs_since(0), 1)


if __name__ == "__main__":
    unittest.main()
