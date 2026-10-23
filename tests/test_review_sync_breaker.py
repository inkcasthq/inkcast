# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #131 (issue #105): the Keycloak sync's circuit breaker.

The breaker exists so that a mass "user not found" (Keycloak restored from an older backup, the wrong issuer, an LDAP
provider switched off) cuts off and erases nobody until an operator confirms, and so that a confirmation applies only
to people who really are gone. Each test is a way that promise breaks. The harness (a mock Keycloak admin API, one
database file the operator's command opens on its own, a clock the test moves) is the PR's own."""

import asyncio
import contextlib
import dataclasses
import io
import unittest

import httpx2
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
        status, _ = self.operator("confirm-deletions", "--pass", pass_id, "--by", "on-call")
        self.assertEqual(status, 0)
        for user in back:
            self.assert_untouched(user)
            self.assertIsNotNone(self.store.find_api_token(pats[user["id"]]), f"{user['kc']}'s PAT still works")
        for user in still_gone:
            self.assert_cut_off(user, deleted_at=T0 + 3600)
            self.assertIsNone(self.store.find_api_token(pats[user["id"]]))


class APercentThatCanNeverTrip(Scenario):
    def test_percent_100_never_trips_so_the_posture_check_must_not_pass_it(self):
        """INKCAST_KEYCLOAK_BREAKER_PERCENT=100 is valid (1-100), but the breaker trips only *above* the percent, and
        more than all of the people read can never be gone: with everyone gone, the reconcile cuts off and (0 days)
        erases everyone. That is the breaker off, so production and staging must be warned as for off, not told
        "pass: holds them all for an operator"."""
        from inkcast.libs.secrets import Redactor, run_checks
        people = self.people(20)
        self.kc.gone.update(u["kc"] for u in people)                # every single person: 100 % of those read
        done = self.reconcile(self.sync(0, keycloak_breaker_percent=100, keycloak_breaker_min=1))
        self.assertEqual((done["erased"], self.trips()), (20, []), "it cannot trip: everyone is erased")
        for environment, status in (("production", "warn"), ("staging", "warn"), ("development", "info")):
            with self.subTest(environment=environment):
                config = dataclasses.replace(self.config(30, keycloak_breaker_percent=100), environment=environment)
                check = {c.id: c for c in run_checks(config, redactor=Redactor(), environ={})}["keycloak-breaker"]
                self.assertEqual(check.status, status)
                self.assertIn("INKCAST_KEYCLOAK_BREAKER_PERCENT=100", check.detail)
                self.assertIn("effectively off", check.detail)


class ConfirmingWhileAReconcileReads(Scenario):
    def test_a_confirmation_during_the_read_phase_does_not_trip_the_breaker_again(self):
        """The reconcile lists everyone, then reads each from Keycloak (minutes in a large realm), then judges. The
        operator confirms the pass while it reads: when it judges, no pass is pending, and the rows it listed before
        the confirmation have no deleted_at, so it counts the five just confirmed as newly gone and records a second
        pass with them, re-opening the breaker and paging again (the PR: "a confirmation cannot re-trip")."""
        people = self.people(20)
        held = people[:5]
        self.kc.gone.update(u["kc"] for u in held)
        mock = self.kc
        confirmed: list[int] = []
        midway = people[10]["kc"]

        def keycloak(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith(f"/users/{midway}") and self.trips() and not confirmed:
                confirmed.append(self.operator("confirm-deletions", "--pass", self.trips()[0]["id"], "--by", "on-call")[0])
            return mock(request)

        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(keycloak))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))
        worker = self.sync(30)
        self.reconcile(worker)                                       # trips; the hook waits for the trip
        pass_id = self.trips()[0]["id"]
        self.reconcile(worker, T0 + 600)                             # the operator confirms halfway through its reads
        self.assertEqual(confirmed, [0], "the confirmation ran, mid-read")
        for user in held:
            self.assert_cut_off(user, deleted_at=T0 + 600)
        self.assertEqual([(t["id"], t["status"]) for t in self.trips()], [(pass_id, "confirmed")], "no second pass")
        self.assertEqual(self.gauge(), 0, "closed: the alert does not page again")


class OperatorInputInTheAudit(Scenario):
    FORGED = "2026-10-22 00:00:00,000 WARNING [Identity] inkcast.identity.sync: Pass pass_0123456789ab dismissed by alice"

    def run_operator(self, *argv: str) -> tuple[object, str]:
        """``self.operator`` with the text log format (the default, and development's) captured: (exit status, or the
        SystemExit code of a refused argument; everything written to stdout, log lines and listing alike)."""
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
            try:
                status, out = self.operator(*argv)
            except SystemExit as exc:
                return exc.code, captured.getvalue()
        return status, captured.getvalue() + out

    def test_a_line_break_in_by_or_pass_cannot_forge_an_audit_line_or_a_row_of_the_listing(self):
        """``--by`` and ``--pass`` are free text from the command line that reach the audit line (the text format
        writes the message as is) and, for ``--by``, the pass row that ``trips`` prints as is, one pass per line (a
        table "a script may read"). A line break in either writes a line that looks like another audit record, or
        another pass; an argument like that is bad usage (exit 2), and nothing is done."""
        people = self.people(20)
        self.kc.gone.update(u["kc"] for u in people[:5])
        self.reconcile(self.sync(30))
        pass_id = self.trips()[0]["id"]
        row = "pass_0123456789ab  2026-10-22T00:00:00Z  5 of 20 gone  pending"
        for argv in (("dismiss-deletions", "--pass", pass_id, "--by", f"on-call\n{row}"),
                     ("dismiss-deletions", "--pass", pass_id, "--by", f"on-call\n{self.FORGED}"),
                     ("confirm-deletions", "--pass", f"nope\n{self.FORGED}")):
            with self.subTest(argv=argv):
                status, written = self.run_operator(*argv)
                self.assertEqual(status, 2, "bad usage")
                self.assertNotIn(self.FORGED, written.splitlines(), "no forged audit line")
                self.assertEqual(self.store.get_breaker_trip(pass_id)["status"], "pending", "nothing done")
                listing = self.operator("trips")[1].splitlines()
                self.assertEqual(len(listing), 1, "one pass, one row")
                self.assertTrue(listing[0].startswith(pass_id))


class TheGaugeSaysWhatTheSyncDoes(Scenario):
    def test_with_the_breaker_off_a_pending_pass_is_not_reported_as_holding_anyone(self):
        """``INKCAST_KEYCLOAK_BREAKER=off`` neither trips nor holds, a pass recorded earlier included: the reconcile cuts
        off and erases as before. The gauge is "1 while the Keycloak user sync holds a pass", and the critical alert
        on it says "Nobody is cut off or erased": both must not say so while the sync is cutting everyone off."""
        people = self.people(20)
        self.kc.gone.update(u["kc"] for u in people[:5])
        self.reconcile(self.sync(0))
        self.assertEqual((len(self.trips()), self.gauge()), (1, 1), "a pass is pending")
        done = self.reconcile(self.sync(0, keycloak_breaker=False), T0 + 600)     # the breaker turned off meanwhile
        self.assertEqual((done["disabled"], done["erased"]), (5, 5), "nothing is held: all five cut off and erased")
        self.assertEqual(self.gauge(), 0, "the sync holds nobody: the alert must not say it does")


if __name__ == "__main__":
    unittest.main()
