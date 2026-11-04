# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #418 (#139, OB3): what the admin-events pass's breaker still lets a spread mass deletion erase.

The harness is ``tests/test_sync_breaker_events.py``'s: a mock Keycloak admin API, one database file, a clock the test
moves. In a realm of a hundred, a script deletes five people every 30 s; the third pass (15 of 100) trips."""

import unittest

from test_sync_breaker import T0
from test_sync_breaker_events import Events


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
