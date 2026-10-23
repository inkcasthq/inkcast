# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #138 (issue #135): the Keycloak sync's breaker holds a mass "disabled" too.

The owner's rule: above the thresholds a disabled person loses sign-in at once, and their personal access tokens are
held, not revoked; confirming the pass revokes them, dismissing it re-enables them. A person the breaker holds only
because Keycloak answered about them *after* the pass (an administrator's own disable, seen while the pass is open)
is not the pass's, so neither decision applies to them: the next reconcile judges them afresh. Each test is a way
that promise breaks. The harness (a mock Keycloak admin API, one database file the operator's command opens on its
own, a clock the test moves, a PAT per person and the REST API over the same database) is the PR's own."""

from test_sync_breaker import T0
from test_sync_breaker_disabled import Held


def _event(at: float, keycloak_id: str) -> dict:
    """A USER UPDATE admin event, as Keycloak stores it (milliseconds)."""
    return {"time": int(at * 1000), "resourceType": "USER", "operationType": "UPDATE", "resourcePath": f"users/{keycloak_id}"}


class TheEventsPassLetsAReEnabledPersonLeaveThePass(Held):
    def test_a_person_an_administrator_enables_and_then_disables_again_is_not_released_by_the_dismissal(self):
        """The importing LDAP provider is switched off: twenty people are held in a pass. An administrator enables
        eve (a USER admin event): the events pass releases her, and her PAT works again. Then the administrator
        disables her on purpose (another event): the events pass holds her, as it holds anyone disabled while the
        breaker is open, "not added to the pass". The operator dismisses the pass (the mass disable was wrong). That
        decision is about the pass's people; eve's disable came after it, from an administrator, about her alone, so
        her PAT must stay refused until the next reconcile judges her afresh. The reconcile, reading her enabled,
        would have taken her out of the pass; the events pass releases her but leaves her in it, so the dismissal
        re-enables the PAT of a person Keycloak has disabled."""
        people = self.people(20)
        self.kc.disabled.update(u["kc"] for u in people)
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        eve = people[7]
        self.kc.disabled.discard(eve["kc"])                              # an administrator enables her
        self.kc.admin_events.append(_event(T0 + 1, eve["kc"]))
        self.tick(worker, T0 + 2)
        self.assertTrue(self.pat_works(eve), "released by the events pass")
        self.kc.disabled.add(eve["kc"])                                  # and disables her again, on purpose
        self.kc.admin_events.append(_event(T0 + 3, eve["kc"]))
        self.tick(worker, T0 + 4)
        self.assert_held(eve)
        self.now = T0 + 100
        self.assertEqual(self.operator("dismiss-deletions", "--pass", pass_id, "--by", "on-call")[0], 0)
        status, body = self.me(eve)
        self.assertEqual((status, body.get("error")), (401, "token_held"),
                         "an administrator disabled eve after the pass: dismissing the pass must not re-enable her PAT")
        self.assert_held(eve)
        for user in people:
            if user is not eve:
                self.assertTrue(self.pat_works(user), f"{user['kc']}: the pass's people are released")
        self.kc.disabled.clear()
        self.kc.disabled.add(eve["kc"])                                  # Keycloak fixed for the others
        self.assertEqual(self.reconcile(worker, T0 + 600)["disabled"], 1, "eve, judged afresh: one person, cut off")
        self.assert_cut_off_disabled(eve)

    def test_a_person_of_a_404_pass_the_events_pass_reads_present_is_not_cut_off_by_the_confirmation(self):
        """A 404 pass: five of twenty gone. Keycloak has ada again (her user-storage provider is back) and an
        administrator saves her (a USER admin event), so the events pass reads her present before any reconcile does.
        The other four really are gone and the operator confirms. "A held person a pass has since read enabled again
        left the pass then" (inkcast identity's own account of a confirmation): ada must keep her row, her token and her
        PAT. The events pass leaves her in the pass, so the confirmation cuts her off, revokes her PAT for good and
        starts her erasure clock."""
        people = self.people(20)
        gone = people[:5]
        self.kc.gone.update(u["kc"] for u in gone)
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        ada = gone[0]
        self.kc.gone.discard(ada["kc"])                                  # Keycloak has her again
        self.kc.admin_events.append(_event(T0 + 1, ada["kc"]))
        self.tick(worker, T0 + 2)
        self.now = T0 + 100
        self.assertEqual(self.operator("confirm-deletions", "--pass", pass_id, "--by", "on-call")[0], 0)
        self.assert_untouched(ada)
        self.assert_working(ada)
        for user in gone[1:]:
            self.assert_cut_off(user, deleted_at=T0 + 100)
