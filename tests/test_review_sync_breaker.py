# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #131 (issue #105): the Keycloak sync's circuit breaker.

The breaker exists so that a mass "user not found" (Keycloak restored from an older backup, the wrong issuer, an LDAP
provider switched off) cuts off and erases nobody until an operator confirms, and so that a confirmation applies only
to people who really are gone. Each test is a way that promise breaks. The harness (a mock Keycloak admin API, one
database file the operator's command opens on its own, a clock the test moves) is the PR's own."""

import unittest

from test_sync_breaker import T0, Scenario


class ConfirmationSparesWhoKeycloakHasAgain(Scenario):
    def test_a_person_the_reconcile_read_as_present_after_the_trip_is_not_cut_off_by_the_confirmation(self):
        """Keycloak is partly fixed after the trip: it has two of the five again. They use personal access tokens
        (automation), so neither signs in. The reconcile while the breaker is open reads both as present, then the
        operator confirms the pass for the three still gone. The confirmation must not cut off the two: Inkcast has
        just read that Keycloak has them, and their PATs would be revoked for good."""
        people = self.people(20)
        held = people[:5]
        self.kc.gone.update(u["kc"] for u in held)
        pats = {u["id"]: self.store.create_api_token(u["id"], "ci", ["inkcast"], None)[0] for u in held}
        worker = self.sync(30)
        self.reconcile(worker)
        pass_id = self.trips()[0]["id"]
        back, still_gone = held[:2], held[2:]
        for user in back:
            self.kc.gone.discard(user["kc"])
        done = self.reconcile(worker, T0 + 600)                      # still open: three 404s; the two read present
        self.assertEqual((done["disabled"], done["erased"]), (0, 0))
        self.now = T0 + 3600
        status, _ = self.operator("confirm-deletions", "--pass", pass_id, "--by", "lateef")
        self.assertEqual(status, 0)
        for user in back:
            self.assert_untouched(user)
            self.assertIsNotNone(self.store.find_api_token(pats[user["id"]]), f"{user['kc']}'s PAT still works")
        for user in still_gone:
            self.assert_cut_off(user, deleted_at=T0 + 3600)
            self.assertIsNone(self.store.find_api_token(pats[user["id"]]))


if __name__ == "__main__":
    unittest.main()
