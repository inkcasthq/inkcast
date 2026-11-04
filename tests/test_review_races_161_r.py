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


if __name__ == "__main__":
    unittest.main()
