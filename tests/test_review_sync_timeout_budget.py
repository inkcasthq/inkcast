# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #178 (issue #152): a per-pass budget of reads about people that get no answer. Each test
failed on the PR's head (4448b9e).

The budget makes a reconcile end early, "as a success for what it did", once three reads got no answer. The PR reads
the people *known* to hang last, so in the steady state the budget is used only after everyone else was read. But
whoever hangs is known only after a read of theirs hung in this process: at start (after every restart), and
whenever several people start hanging at once (an LDAP referral server going away), they are spread through the
reconcile's order and the budget ends it with people who would answer still untried. Keycloak ids carry non-hex
letters (``kc-p00``) so an Inkcast id (``usr_<hex>``) can't contain one by chance."""

import unittest

import httpx2
from test_sync_person_timeouts import T0, Scenario


class TheBreakerCountsEveryoneOrNobody(Scenario):
    """The breaker (#105) holds a mass "gone": it counts the people newly found gone against those read, and trips at
    ``keycloak_breaker_min`` (5) and more than ``keycloak_breaker_percent`` (10 %). Its rule is that everyone is read
    before anyone is acted on. A reconcile the budget ends early has read a slice, and the PR judges the slice: a
    realm restored from an older backup (half the people answer 404) with three people whose reads hang early in the
    order shows the breaker two people gone, under the minimum, so those two are cut off, and at
    ``INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=0`` erased, though the realm-wide count is ten and trips it at the next read."""

    def test_a_reconcile_the_budget_ends_early_does_not_act_on_a_slice_the_breaker_could_not_count(self):
        people = self.people(*(f"kc-p{i:02d}" for i in range(20)))
        h1, g1, h2, g2, h3 = people[:5]                             # by Inkcast id: the hangers spread out
        gone = [g1, g2, *people[5:13]]                              # ten of twenty answer 404: an old backup
        for user in (h1, h2, h3):
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        self.kc.gone.update(user["kc"] for user in gone)
        worker = self.sync(0)                                       # erased at the first reconcile that finds them gone
        self.reconcile(worker, T0)                                  # at start: nobody is known to hang yet
        self.assertEqual(self.pass_errors, [])
        for user in gone:
            self.assert_untouched(user)                             # g1 and g2: erased by a breaker that saw two
        self.reconcile(worker, T0 + 600)                            # the hangers are read last now: everyone counted
        trips = self.store.breaker_trips()
        self.assertEqual(len(trips), 1, "the realm-wide count trips the breaker")
        self.assertEqual(trips[0]["gone"], 10)
        for user in gone:
            self.assert_untouched(user)
        self.assertEqual(self.pass_errors, [])


class AReconcileTheBudgetEndedIsNotAReconcileOfEveryone(Scenario):
    """The PR advances ``last_reconcile`` after a reconcile the budget ended early, so the people it left untried
    wait a whole interval, and each reconcile learns only the three people whose reads used its budget. With six
    people who hang spread through the order (none next to another: #121 read them all, each costing its timeout),
    a person disabled in Keycloak with no admin event (an LDAP-imported user disabled in the directory: the
    reconcile is the only path) who sorts after them is cut off at the third reconcile, two intervals later; with
    3n hangers, n intervals."""

    def test_a_person_after_people_not_yet_known_to_hang_is_cut_off_within_one_reconcile_interval(self):
        people = self.people(*(f"kc-p{i:02d}" for i in range(12)))
        hanging, disabled = people[0:11:2], people[11]              # positions 0, 2, ..., 10 hang; 11 is disabled
        for user in hanging:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        self.kc.disabled.add(disabled["kc"])
        worker = self.sync(30, reconcile_seconds=600)
        self.reconcile(worker, T0)                                  # at start (or after a restart): none known
        for at in range(30, 601, 30):                               # one reconcile interval of passes
            if self.row(disabled)["disabled_at"] is not None:
                break
            self.tick(worker, T0 + at)
        self.assertEqual(self.pass_errors, [])
        self.assert_cut_off(disabled)
        for user in hanging:
            self.assert_untouched(user)


if __name__ == "__main__":
    unittest.main()
