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

import asyncio
import unittest

import httpx2
from test_sync_breaker import Scenario as BreakerScenario
from test_sync_person_timeouts import T0, Keycloak, Scenario


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


class AConfirmationAfterAReconcileTheBudgetEnded(BreakerScenario):
    """While the breaker holds a pass, a reconcile that reads a held person as present releases them from it, so an
    operator's confirmation does not cut them off (review of #131). People the budget left untried are not read, and
    with ``last_reconcile`` advanced they stay unread for the interval: a confirmation then cuts off, and revokes the
    personal access tokens of, people Keycloak has again."""

    def setUp(self):
        super().setUp()
        self.kc = Keycloak()                                        # the breaker's harness, with reads that hang
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.kc))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))

    def test_does_not_cut_off_people_keycloak_has_again_whom_the_budget_left_unread(self):
        people = self.people(20)
        held = people[5:10]
        self.kc.gone.update(user["kc"] for user in held)
        worker = self.sync(30)
        self.reconcile(worker)                                      # five of twenty gone: the breaker trips
        pass_id = self.trips()[0]["id"]
        for user in held:                                           # Keycloak is fixed: it has all five again
            self.kc.gone.discard(user["kc"])
        for user in people[0:5:2]:                                  # meanwhile three people start hanging
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        self.reconcile(worker, T0 + 600)
        for at in range(630, 901, 30):
            self.tick(worker, T0 + at)
        status, _ = self.operator("confirm-deletions", "--pass", pass_id, "--by", "on-call")
        self.assertEqual(status, 0)
        for user in held:
            self.assert_untouched(user)


class ThePeopleWhoHangDelayEachOtherOnly(Scenario):
    """The PR reads the people known to hang last "so they delay each other only". But the reconcile reads the
    DELETE_ACCOUNT events of the people found gone after everyone's ``users/{id}``, and skips them once the budget
    is used: with three people known to hang, at every reconcile, so nobody found gone has their events read. A
    self-deletion the events pass missed (a replica's clock running fast, a burst of more than 500) then counts for
    the breaker as an administrator's deletion: five of them trip it, and the open breaker holds every 404 (their
    tokens keep working) and every ``enabled: false`` until an operator dismisses a pass that held nothing to hold."""

    def test_the_events_of_the_people_found_gone_are_read_before_the_people_who_hang(self):
        people = self.people(*(f"kc-p{i:02d}" for i in range(20)))
        hanging = people[0:5:2]                                     # 0, 2, 4: none next to another
        deleted_own = [people[1], people[3], *people[5:8]]          # five who deleted their own accounts
        for user in hanging:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        worker = self.sync(30, reconcile_seconds=600)
        self.reconcile(worker, T0)                                  # at start: the three become known to hang
        self.assertEqual(self.pass_errors, [])
        self.store.set_sync_state("keycloak-user-events", str(int((T0 + 600) * 1000) + 120_000))   # a replica ran fast
        for user in deleted_own:
            self.kc.gone.add(user["kc"])
            self.kc.events.append(self.event(user["kc"], 590_000))  # before the cursor's look-back: missed
        done = self.reconcile(worker, T0 + 600)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(self.store.breaker_trips(), [], "five self-deletions counted as an administrator's")
        self.assertEqual(done["erased"], 5, "erased: they asked, and the event is specific")
        for user in deleted_own:
            self.assertIsNone(self.row(user))
        for user in hanging:
            self.assert_untouched(user)


if __name__ == "__main__":
    unittest.main()
