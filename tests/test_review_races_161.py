# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #161: the erasure and cut-off races left after #102 (the adversarial review of PR #160).

Each race is driven deterministically with ``tests/test_erasure_recheck.py``'s harness: a second ``Store`` on the
same database file stands in for another replica, and its write (a sign-in, a release, a cut-off, a whole pass)
lands at the exact moment between a check and the act that relied on it, by wrapping the act's store method. On
main every raced test fails on its assertions: the person is held, cut off, or told the wrong thing."""

import unittest

from inkcast.breaker import _confirm
from inkcast.services.identity.keycloak_sync import KeycloakSync
from test_erasure_recheck import Raced
from test_sync_breaker import T0


class Base(Raced):
    def pat(self, user: dict) -> str:
        return self.store.create_api_token(user["id"], "ci", ["posts:read"], None)[0]

    def sign_in(self, user: dict) -> dict:
        """Keycloak has the person again, enabled, and let them in: another replica commits their sign-in."""
        self.kc.disabled.discard(user["kc"])
        return super().sign_in(user)

    def hook(self, store, method: str, act, after: bool = False) -> list:
        """Run ``act`` once, around the first call of ``store.<method>`` (before it, or ``after`` it returned): the
        moment between a caller's check and its act. Returns the list ``act``'s result lands in."""
        real, landed = getattr(store, method), []

        def wrapped(*args, **kwargs):
            if not after and not landed:
                landed.append(act())
            result = real(*args, **kwargs)
            if after and not landed:
                landed.append(act())
            return result
        setattr(store, method, wrapped)
        self.addCleanup(delattr, store, method)
        return landed

    def other_sync(self, days: int = 30) -> KeycloakSync:
        """A sync worker on the other connection, as another replica's would be; ``tick`` runs its reconcile."""
        worker = KeycloakSync(self.config(days), self.other, self.http, None, clock=lambda: self.now)
        worker.last_reconcile = 0
        return worker

    def confirm(self, pass_id: str) -> list:
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.assertEqual(_confirm(self.store, pass_id, "on-call", T0 + 3600), 0)
        return logs.records

    def confirmed(self, records: list):
        (line,) = [r for r in records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"]
        return line

    def assert_held(self, user: dict) -> None:
        row = self.row(user)
        self.assertIsNotNone(row.get("held_at"), f"{user['kc']} is held")
        self.assertIsNone(row["disabled_at"], f"{user['kc']} is not cut off")
        self.assertIsNone(self.store.get_token(f"ic_at_{user['kc']:x<43}", "access"), f"{user['kc']}'s OAuth token is cut")

    def assert_working(self, user: dict, pat: str, before: dict, signed_in: list) -> None:
        """Neither held nor cut off, every row kept, the sign-in standing, the PAT and the OAuth token working."""
        self.assert_intact(user, before, signed_in)
        self.assertIsNotNone(self.store.find_api_token(pat), "their PAT works: neither held nor revoked")
        self.assertIsNotNone(self.store.get_token(f"ic_at_{user['kc']:x<43}", "access"), "their OAuth token lives")
        self.assertEqual(self.counted("inkcast_identity_users_disabled_total"), {}, "nobody counted as cut off")


# ---- 1. The hold's check-then-act race: ``_hold`` checked for a sign-in, ``hold_user`` did not; the events pass's
#         ``hold_person`` never checked ----------------------------------------------------------------------------

class TheStoreHold(Base):
    """``hold_user`` with ``last_login_at``: nothing unless it is still their last sign-in, as ``disable_user``."""

    def test_a_sign_in_since_the_row_was_read_holds_nothing(self):
        ada = self.person("kc-ada")
        pat = self.pat(ada)
        back = self.sign_in(ada)                                        # another replica's sign-in
        self.assertIsNone(self.store.hold_user(ada["id"], T0, last_login_at=ada["last_login_at"]), "not as read: nothing")
        self.assertIsNone(self.row(ada)["held_at"])
        self.assertIsNotNone(self.store.find_api_token(pat), "her PAT works")
        self.assertIsNotNone(self.store.get_token(f"ic_at_{ada['kc']:x<43}", "access"), "her token lives")
        self.assertEqual(self.store.hold_user(ada["id"], T0, last_login_at=back["last_login_at"]), {"oauth_tokens": 1, "codes": 0},
                         "as they stand now: held")
        self.assertEqual(self.row(ada)["held_at"], T0)
        self.assertIsNone(self.store.find_api_token(pat), "refused while held")


class TheReconcileHold(Base):
    """``_reconcile``: everyone read, the breaker trips on the disabled, then each is held."""

    def test_a_sign_in_between_the_check_and_the_hold_is_not_held(self):
        people = [self.person(f"kc-p{i:02d}") for i in range(20)]
        pats = {u["id"]: self.pat(u) for u in people}
        self.kc.disabled.update(u["kc"] for u in people)
        ada = people[0]
        before = self.person_rows(ada["id"])
        signed_in = self.sign_in_at(self.store, "hold_user", ada)        # after the reconcile's sign-in check
        done = self.reconcile(self.sync(30))
        self.assertEqual((done["disabled"], done["erased"]), (0, 0))
        (trip,) = self.trips()
        self.assert_working(ada, pats[ada["id"]], {**before, "sync_breaker_people": 1}, signed_in)   # recorded before the hold
        for user in people[1:]:
            self.assert_held(user)
        self.assertEqual(self.confirmed(self.confirm(trip["id"])).skipped, 1, "the confirmation spares her: signed in since")
        self.assertIsNotNone(self.store.find_api_token(pats[ada["id"]]))
        self.assertIsNone(self.row(ada)["disabled_at"])


class TheEventsPassHold(Base):
    """``tick``'s admin-events pass while a pass is held: Keycloak's answer about a changed person, then the hold."""

    def test_a_sign_in_between_the_read_and_the_hold_is_not_held(self):
        people = [self.person(f"kc-p{i:02d}") for i in range(20)]
        bob = self.person("kc-bob")                                     # enabled when the breaker trips
        pat = self.pat(bob)
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.kc.disabled.update(u["kc"] for u in people)
        self.reconcile(worker, T0 + 1)
        self.assertEqual(len(self.trips()), 1, "held")
        self.kc.disabled.add(bob["kc"])                                 # an administrator disables him too
        self.kc.admin_events.append({"time": int(self.now * 1000) + 1000, "resourceType": "USER", "operationType": "UPDATE",
                                     "resourcePath": f"users/{bob['kc']}"})
        before = self.person_rows(bob["id"])
        signed_in = self.sign_in_at(self.store, "hold_user", bob)        # between the read and the hold
        done = self.tick(worker, T0 + 3)
        self.assertEqual((done["checked"], done["disabled"]), (1, 0))
        self.assert_working(bob, pat, before, signed_in)
        for user in people:
            self.assert_held(user)


if __name__ == "__main__":
    unittest.main()
