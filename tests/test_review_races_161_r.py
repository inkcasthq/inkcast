# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The review of PR #397 (issue #161): what its tests do not drive.

The harness is ``tests/test_review_races_161.py``'s (``tests/test_erasure_recheck.py``'s ``Raced``: a second ``Store``
on the same database file for another process, the operator's ``inkcast identity`` command here)."""

import asyncio
import unittest

import httpx2

from inkcast.breaker import _confirm
from inkcast.libs.db.identity import BREAKER_CONFIRMED, BREAKER_GONE
from inkcast.services.identity.keycloak_sync import KeycloakSync
from test_review_races_161 import Base
from test_sync_breaker import T0


class TheReleasesCost(Base):
    """``release_breaker_people`` runs at every reconcile and for every person an admin event names, inside
    ``BEGIN IMMEDIATE``; a realm purge puts everyone in a pass. A statement that scans ``sync_breaker_people`` once
    per person is quadratic under the write lock: 20 000 held and read present held it for 3.5 s on the PR (42 ms on
    main, whose DELETE used the table's key)."""

    def plans(self, *args) -> list[str]:
        conn, statements = self.store._conn, []
        conn.set_trace_callback(lambda sql: statements.append(sql) if "sync_breaker_people" in sql else None)
        try:
            self.store.release_breaker_people(*args)
        finally:
            conn.set_trace_callback(None)
        self.assertTrue(statements, "it touched the table")
        return [row[3] for sql in statements for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]

    def test_no_statement_scans_the_table(self):
        ada, bob = self.person("kc-ada"), self.person("kc-bob")
        pass_id = self.store.record_breaker_trip(T0, 2, 10, [(ada["id"], ada["last_login_at"]), (bob["id"], bob["last_login_at"])])
        self.assertTrue(self.store.resolve_breaker_trip(pass_id, BREAKER_CONFIRMED, T0 + 1, "on-call"))
        for args in (([ada["id"], "usr_nobody"],), ([bob["id"]], BREAKER_GONE)):
            with self.subTest(args=args):
                plans = self.plans(*args)
                self.assertEqual([p for p in plans if p.startswith("SCAN sync_breaker_people")], [], plans)
        self.assertEqual(self.store.breaker_trip_people(pass_id), [], "both released, the pass claimed or not")


class AConfirmationDuringTheReconcilesReads(Base):
    """Issue #161, item 2: "A reconcile that read them active just before the operator confirms can lose that
    release." The reconcile reads everyone before it acts (one Keycloak request per person: minutes for thousands),
    so the confirmation lands most often while it is still reading the people after them. The PR's tests run the
    other pass whole inside the confirmation; here the confirmation runs whole inside the reconcile's reads."""

    def raced(self, kind: str) -> None:
        people = sorted((self.person(f"kc-p{i:02d}") for i in range(20)), key=lambda u: u["id"])   # as the reconcile reads
        held = people[:5]
        pats = {u["id"]: self.pat(u) for u in held}
        (self.kc.gone if kind == "gone" else self.kc.disabled).update(u["kc"] for u in held)
        self.reconcile(self.sync(30))
        (trip,) = self.trips()
        self.kc.gone.clear()                                             # Keycloak is fixed: the realm restored,
        self.kc.disabled.clear()                                         # everyone enabled again
        last = people[-1]["kc"]                                          # read after every person the pass holds
        confirmed = []

        def keycloak(request):
            if request.url.path.endswith(f"/users/{last}") and not confirmed:   # the held were read present already
                confirmed.append(_confirm(self.other, trip["id"], "on-call", T0 + 3600))
            return self.kc(request)
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(keycloak))
        self.addCleanup(lambda: asyncio.run(http.aclose()))
        worker = KeycloakSync(self.config(30), self.store, http, None, clock=lambda: self.now)
        self.reconcile(worker, T0 + 3600)
        self.assertEqual(confirmed, [0], "the operator's confirmation ran during the reconcile's reads")
        for user in held:
            row = self.row(user)
            self.assertIsNone(row["disabled_at"], f"{user['kc']} was read present before the confirmation: not cut off")
            self.assertIsNone(row["deleted_at"], f"{user['kc']}: no grace period runs")
            self.assertIsNotNone(self.store.find_api_token(pats[user["id"]]), f"{user['kc']}'s PAT works")

    def test_a_gone_person_read_present_before_the_confirmation_is_not_cut_off(self):
        self.raced("gone")

    def test_a_disabled_person_read_enabled_before_the_confirmation_keeps_their_pats(self):
        self.raced("disabled")


class WhatThePRsTestsLeaveUnpinned(Base):
    """Behaviour fbf1764 has right that no test pins: each test passes on fbf1764 and fails under the mutant named."""

    def test_the_events_pass_alone_releases_a_person_read_present_from_a_claimed_pass(self):
        """``TheRestoreRacingAConfirmation``'s events variant runs the reconcile too (its tick is at T0 + 3600 with
        ``last_reconcile`` at T0), so the reconcile's release spares them there; mutant: the events pass releases
        only ``if held``, as before #161."""
        people = sorted((self.person(f"kc-p{i:02d}") for i in range(20)), key=lambda u: u["id"])
        gone = people[:5]
        self.kc.gone.update(u["kc"] for u in gone)
        self.reconcile(self.sync(30))
        (trip,) = self.trips()
        self.kc.gone.clear()
        self.now = T0 + 3600
        worker = self.other_sync(30)
        worker.last_reconcile = self.now                                 # not due: the admin-events pass alone
        for user in gone:
            self.kc.admin_events.append({"time": int(self.now * 1000) + 1000, "resourceType": "USER",
                                         "operationType": "UPDATE", "resourcePath": f"users/{user['kc']}"})
        self.kc.read.clear()
        landed = self.hook(self.store, "breaker_trip_people", lambda: self.tick(worker), after=True)
        self.confirm(trip["id"])
        self.assertEqual(sorted(self.kc.read), sorted(u["kc"] for u in gone), "only the edited were read: no reconcile")
        self.assertEqual(landed[0]["checked"], 5)
        for user in gone:
            self.assert_untouched(user)

    def test_a_person_still_disabled_at_the_next_reconcile_stays_in_the_pass_and_is_cut_off(self):
        """The reverse of a release: a person Keycloak still has disabled is read so at every reconcile while the pass
        waits, and must not leave it, or the confirmation spares a person Keycloak really disabled; mutant: the
        release drops its ``kind``."""
        people = [self.person(f"kc-p{i:02d}") for i in range(20)]
        disabled = people[:5]
        pats = {u["id"]: self.pat(u) for u in disabled}
        self.kc.disabled.update(u["kc"] for u in disabled)
        worker = self.sync(30)
        self.reconcile(worker)
        (trip,) = self.trips()
        self.reconcile(worker, T0 + 600)                                 # still disabled: still held, still in the pass
        self.assertEqual(len(self.store.breaker_trip_people(trip["id"])), 5)
        line = self.confirmed(self.confirm(trip["id"]))
        self.assertEqual((line.applied, line.disabled, line.skipped), (5, 5, 0))
        for user in disabled:
            self.assert_cut_off(user)
            self.assertIsNone(self.store.find_api_token(pats[user["id"]]), "their PATs are revoked")

    def test_a_return_visit_whose_erasure_a_pass_elsewhere_declined_is_not_told_it_signed_in(self):
        """``/account/return``: the erasure declines because a pass elsewhere marked her (``deleted_at``), with no
        sign-in: the plain finishing page, never "you signed in again"; mutant: the sign-in page always."""
        from test_review_races_161 import TheReturnVisit
        ada = self.person("kc-ada")
        cookie = TheReturnVisit.armed(self, ada)
        ctx, client = TheReturnVisit.app(self)
        self.kc.gone.add(ada["kc"])
        landed = self.hook(ctx.store, "erase_user", lambda: self.other.mark_user_deleted(ada["id"], T0))
        with client:
            page = client.get("/account/return", cookies={"inkcast_deletion": cookie})
        self.assertEqual((page.status_code, landed), (200, [T0]))
        self.assertIsNotNone(ctx.store.get_user(ada["id"]), "nothing erased: not as read")
        self.assertIn("once the sign-in provider confirms the account is gone", page.text)
        self.assertNotIn("you signed in again", page.text)
        self.assertIsNotNone(self.store.deletion_request_by_cookie(cookie), "the record stays")


class TheConfirmationsAuditForTheDisabled(Base):
    """Review of #397, C1: a disabled person cut off by another pass between the confirmation's check and its own
    cut-off was counted as skipped ("signed in since, present or enabled again, or erased already") although she is
    cut off. She is counted apart, as the gone kind's ``marked`` is."""

    def test_a_disabled_person_cut_off_elsewhere_between_the_check_and_the_act_is_not_skipped(self):
        people = sorted((self.person(f"kc-p{i:02d}") for i in range(20)), key=lambda u: u["id"])
        held = people[:5]
        self.kc.disabled.update(u["kc"] for u in held)
        self.reconcile(self.sync(30))
        (trip,) = self.trips()
        ada = held[0]
        self.hook(self.store, "disable_user", lambda: self.other.disable_user(ada["id"]))
        line = self.confirmed(self.confirm(trip["id"]))
        self.assertIsNotNone(self.row(ada)["disabled_at"], "she is cut off")
        self.assertEqual((line.applied, line.disabled, line.elsewhere, line.skipped), (4, 4, 1, 0))


if __name__ == "__main__":
    unittest.main()
