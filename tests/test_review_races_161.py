# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #161: the erasure and cut-off races left after #102 (the adversarial review of PR #160).

Each race is driven deterministically with ``tests/test_erasure_recheck.py``'s harness: a second ``Store`` on the
same database file stands in for another replica, and its write (a sign-in, a release, a cut-off, a whole pass)
lands at the exact moment between a check and the act that relied on it, by wrapping the act's store method. On
main every raced test fails on its assertions: the person is held, cut off, or told the wrong thing."""

import unittest

from inkcast.breaker import _confirm
from inkcast.libs.db.identity import BREAKER_CONFIRMED
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


# ---- 2. A restore lost when it races a breaker confirmation: a gone person's "back" was recorded only in
#         ``sync_breaker_people``, and ``release_breaker_people`` touched pending passes only ---------------------

class TheStoreRelease(Base):
    """``release_breaker_people`` lands whatever the pass's status; ``claim_breaker_person`` is the confirmation's
    one write per person before it acts, true once."""

    def test_a_release_reaches_a_claimed_pass_and_a_claim_is_taken_once(self):
        ada, bob = self.person("kc-ada"), self.person("kc-bob")
        pass_id = self.store.record_breaker_trip(T0, 2, 10, [(ada["id"], ada["last_login_at"]), (bob["id"], bob["last_login_at"])])
        self.assertTrue(self.store.resolve_breaker_trip(pass_id, BREAKER_CONFIRMED, T0 + 1, "on-call"))
        self.assertEqual(self.other.release_breaker_people([ada["id"], "usr_nobody"]), 1,
                         "a reconcile read her present: she leaves the pass, claimed or not")
        self.assertEqual([p["user_id"] for p in self.store.breaker_trip_people(pass_id)], [bob["id"]])
        self.assertFalse(self.store.claim_breaker_person(pass_id, ada["id"]), "released: the confirmation leaves her")
        self.assertTrue(self.store.claim_breaker_person(pass_id, bob["id"]), "the pass held him: the confirmation acts")
        self.assertFalse(self.store.claim_breaker_person(pass_id, bob["id"]), "once")
        self.assertEqual(self.other.release_breaker_people([bob["id"]]), 0, "too late: claimed")
        self.assertEqual(self.store.breaker_trip_people(pass_id), [])


class TheRestoreRacingAConfirmation(Base):
    """A pending pass of gone people, and the realm restored: Keycloak has them all again. Another replica's pass
    reads them present after the operator's confirmation claimed the pass (and listed its people), before it acts
    on them: the pass no longer holds them, so nobody is cut off."""

    def raced(self, method: str, events: bool) -> None:
        people = [self.person(f"kc-p{i:02d}") for i in range(20)]
        gone, kept = people[:5], people[5:]
        self.kc.gone.update(u["kc"] for u in gone)
        self.reconcile(self.sync(30))
        (trip,) = self.trips()
        before = {u["id"]: self.person_rows(u["id"]) for u in gone}
        self.kc.gone.clear()                                             # the realm is restored

        def other_pass() -> dict:
            worker = self.other_sync(30)
            if events:                                                   # the admin-events pass alone, with an edit of each
                worker.last_reconcile = self.now
                for user in gone:
                    self.kc.admin_events.append({"time": int(self.now * 1000) + 1000, "resourceType": "USER",
                                                 "operationType": "UPDATE", "resourcePath": f"users/{user['kc']}"})
            return self.tick(worker, T0 + 3600)
        landed = self.hook(self.store, method, other_pass, after=True)
        records = self.confirm(trip["id"])
        self.assertEqual(len(landed), 1, "the other replica's pass ran inside the confirmation")
        self.assertEqual(landed[0]["disabled"], 0, "it cut nobody off either: Keycloak has them")
        for user in gone:
            self.assert_untouched(user)
            self.assertEqual(self.person_rows(user["id"]), {**before[user["id"]], "sync_breaker_people": 0}, "every row kept")
        for user in kept:
            self.assert_untouched(user)
        self.assertEqual([r for r in records if getattr(r, "event", "") == "identity.user.disabled"], [], "no cut-off line")
        self.assertEqual(self.confirmed(records).applied, 0, "the pass held nobody any more")
        self.assertEqual(self.store.breaker_trip_people(trip["id"]), [])

    def test_a_reconcile_that_reads_them_present_as_the_pass_is_claimed_spares_them(self):
        for method in ("resolve_breaker_trip", "breaker_trip_people"):
            with self.subTest(after=method):
                self.setUp()
                self.raced(method, events=False)

    def test_an_events_pass_that_reads_them_present_as_the_pass_is_claimed_spares_them(self):
        self.raced("breaker_trip_people", events=True)


# ---- 3. Skip accounting: a cut-off that declined after the mark had started the clock was counted as skipped ----

class TheConfirmationsAudit(Base):
    """``inkcast identity confirm-deletions`` marks a gone person gone (the erasure clock) and then cuts them off, in
    two transactions. When the cut-off declines after the mark, the audit line says whether the clock runs."""

    def trip(self, gone: list[dict]) -> str:
        self.kc.gone.update(u["kc"] for u in gone)
        self.reconcile(self.sync(30))
        (trip,) = self.trips()
        return trip["id"]

    def test_a_cut_off_elsewhere_between_the_mark_and_the_cut_off_is_marked_gone_not_skipped(self):
        people = [self.person(f"kc-p{i:02d}") for i in range(20)]
        gone = people[:5]
        pass_id = self.trip(gone)
        ada = min(gone, key=lambda u: u["id"])                           # the first the confirmation acts on
        # The breaker closed with the claim: a reconcile on another replica, finding her 404 alone, cuts her off
        # between the confirmation's mark and its cut-off, which declines (disabled_at is set).
        landed = self.hook(self.store, "disable_user", lambda: self.other.disable_user(ada["id"]))
        records = self.confirm(pass_id)
        self.assertEqual(len(landed), 1)
        self.assertIsNotNone(landed[0], "the other replica's cut-off landed first")
        for user in gone:
            self.assert_cut_off(user, deleted_at=T0 + 3600)             # her clock runs from the confirmation's mark
        line = self.confirmed(records)
        self.assertEqual((line.applied, line.gone, line.marked, line.skipped), (4, 4, 1, 0),
                         "marked gone, not cut off here: not skipped, her grace period runs")
        self.assertIn("marked gone", line.getMessage())
        self.assertEqual(self.store.breaker_trip_people(pass_id), [])

    def test_a_sign_in_between_the_mark_and_the_cut_off_is_skipped_as_the_sign_in_stopped_the_clock(self):
        people = [self.person(f"kc-p{i:02d}") for i in range(20)]
        gone = people[:5]
        pass_id = self.trip(gone)
        ada = min(gone, key=lambda u: u["id"])
        before = self.person_rows(ada["id"])
        signed_in = self.sign_in_at(self.store, "disable_user", ada)     # Keycloak has her again and let her in
        records = self.confirm(pass_id)
        self.assert_intact(ada, {**before, "sync_breaker_people": 0}, signed_in)   # deleted_at None: no clock runs
        for user in gone:
            if user is not ada:
                self.assert_cut_off(user, deleted_at=T0 + 3600)
        line = self.confirmed(records)
        self.assertEqual((line.applied, line.marked, line.skipped), (4, 0, 1), "the sign-in stopped the clock: skipped")


if __name__ == "__main__":
    unittest.main()
