# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #158 (issue #102): a person who signs in, or is restored, between the decision and the
transaction is never cut off, on any path.

The sync's cut-offs compared ``last_login_at`` with a row read after Keycloak's answer (``_cut_off`` re-read it "as it
stands now"), so a sign-in committed by another replica between that answer and the re-read was taken for the row as
read: the person who had just signed in was cut off, their personal access tokens revoked for good. Each test commits
the sign-in through a second ``Store`` on the same database file at that moment, with ``tests/test_erasure_recheck.py``'s
harness."""

import unittest

from test_erasure_recheck import Raced
from test_sync_breaker import T0


class Base(Raced):
    def pat(self, user: dict) -> str:
        return self.store.create_api_token(user["id"], "ci", ["posts:read"], None)[0]

    def back(self, user: dict) -> dict:
        """Keycloak has the person again, enabled, and let them in: another replica commits their sign-in."""
        self.kc.disabled.discard(user["kc"])
        return self.sign_in(user)

    def after_the_answer(self, worker, user: dict) -> list[dict]:
        """Keycloak answers the pass about ``user``; then their sign-in commits on the other connection, before the
        pass acts on that answer. Returns the list the sign-in's row lands in."""
        real, signed_in = worker.person, []

        async def person(keycloak_id):
            state = await real(keycloak_id)
            if keycloak_id == user["kc"] and not signed_in:
                signed_in.append(self.back(user))
            return state
        worker.person = person
        return signed_in

    def after_the_check(self, worker, user: dict) -> list[dict]:
        """The reconcile's ``_signed_in_since`` finds no sign-in for ``user``; theirs commits right after it."""
        real, signed_in = worker._signed_in_since, []

        def since(listed):
            answer = real(listed)
            if listed["id"] == user["id"] and not signed_in:
                signed_in.append(self.back(user))
            return answer
        worker._signed_in_since = since
        return signed_in

    def assert_not_cut_off(self, user: dict, before: dict, signed_in: list[dict], pat: str) -> None:
        self.assert_intact(user, before, signed_in)
        self.assertIsNotNone(self.store.find_api_token(pat), "their PAT works: not revoked")
        self.assertIsNotNone(self.store.get_token(f"ic_at_{user['kc']:x<43}", "access"), "their token lives")
        self.assertEqual(self.counted("inkcast_identity_users_disabled_total"), {}, "nobody counted as cut off")


class TheEventsPassCutOff(Base):
    """``tick``'s admin-events pass: Keycloak's answer about a changed person, then the cut-off."""

    def changed(self, user: dict) -> None:
        self.kc.admin_events.append({"time": int(self.now * 1000) + 1000, "resourceType": "USER", "operationType": "UPDATE",
                                     "resourcePath": f"users/{user['kc']}"})

    def raced(self, kind: set) -> None:
        ada, bob = self.person("kc-ada"), self.person("kc-bob")
        pat = self.pat(ada)
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        kind.add(ada["kc"])
        self.changed(ada)
        before = self.person_rows(ada["id"])
        signed_in = self.after_the_answer(worker, ada)
        done = self.tick(worker, T0 + 2)
        self.assertEqual((done["checked"], done["disabled"]), (1, 0))
        self.assert_not_cut_off(ada, before, signed_in, pat)
        self.assert_untouched(bob)

    def test_a_sign_in_after_keycloaks_404_is_not_cut_off(self):
        self.raced(self.kc.gone)

    def test_a_sign_in_after_keycloaks_disabled_answer_is_not_cut_off(self):
        self.raced(self.kc.disabled)

    def test_unchanged_the_events_pass_still_cuts_off(self):
        ada = self.person("kc-ada")
        worker = self.sync(30)
        self.tick(worker)
        self.kc.gone.add(ada["kc"])
        self.changed(ada)
        self.assertEqual(self.tick(worker, T0 + 2)["disabled"], 1)
        self.assert_cut_off(ada, deleted_at=T0 + 2)


class TheReconcileCutOff(Base):
    """``_reconcile``: everyone read, then the cut-off of the disabled and of the gone with no event."""

    def test_a_sign_in_after_keycloaks_disabled_answer_is_not_cut_off(self):
        ada, bob = self.person("kc-ada"), self.person("kc-bob")
        pat = self.pat(ada)
        worker = self.sync(30)
        self.kc.disabled.add(ada["kc"])                             # one of two: below the breaker's minimum
        before = self.person_rows(ada["id"])
        signed_in = self.after_the_answer(worker, ada)
        done = self.reconcile(worker)
        self.assertEqual((done["disabled"], done["erased"]), (0, 0))
        self.assert_not_cut_off(ada, before, signed_in, pat)
        self.assert_untouched(bob)

    def test_a_sign_in_after_the_signed_in_check_is_not_cut_off_whatever_the_grace_period(self):
        for days in (30, 0):
            with self.subTest(days=days):
                self.setUp()
                ada, bob = self.person("kc-ada"), self.person("kc-bob")
                pat = self.pat(ada)
                worker = self.sync(days)
                self.kc.gone.add(ada["kc"])                         # an administrator deleted her: no event
                before = self.person_rows(ada["id"])
                signed_in = self.after_the_check(worker, ada)
                done = self.reconcile(worker)
                self.assertEqual((done["disabled"], done["erased"]), (0, 0))
                self.assert_not_cut_off(ada, before, signed_in, pat)
                self.assert_untouched(bob)

    def test_unchanged_the_reconcile_still_cuts_off_and_erases(self):
        ada, bob = self.person("kc-ada"), self.person("kc-bob")
        worker = self.sync(0)
        self.kc.gone.add(ada["kc"])
        self.kc.disabled.add(bob["kc"])
        self.assertEqual(self.reconcile(worker)["disabled"], 2)
        self.assert_erased_once(ada, "reconcile")
        self.assert_cut_off(bob)


if __name__ == "__main__":
    unittest.main()
