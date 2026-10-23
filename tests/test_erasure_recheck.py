# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #102: an erasure re-checks, inside its own transaction, the conditions its caller relied on.

Every erase path reads the person, decides, and then ``Store.erase_user`` deletes in a transaction of its own. A
sign-in handled by another replica (or another process in split mode) that commits between the decision and that
transaction is the one proof Inkcast has that Keycloak has the person; #101 compared ``last_login_at`` with the row
as listed, inside one process, and left the window between that comparison and the transaction open. Now every
caller passes the row as it read it, ``erase_user`` reads it again under the write lock and erases nothing unless
it still stands as read; ``disable_user`` and ``mark_user_deleted`` do the same with ``last_login_at`` for a breaker
confirmation, which cuts a person off and starts their erasure clock on the pass's say-so.

Each test commits a sign-in through a second ``Store`` on the same database file at the moment between the check and
the transaction (the act's first store call is wrapped, as another replica's sign-in callback would land there) and
expects the person intact: every row kept, nothing counted, no audit record. Then the same path with nothing in
between, which erases exactly once. The harness (a mock Keycloak admin API, one database file, a clock the test
moves, the real metrics registry) is ``tests/test_sync_breaker.py``'s. On main every raced test fails on its
assertions: the person is erased, or cut off, all the same."""

import asyncio
import unittest

import httpx2
from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.db import Store
from inkcast.libs.db.erasure import USER_TABLES
from inkcast.libs.http.app import compose
from inkcast.services.identity.keycloak_sync import USER_EVENTS_CURSOR
from test_sync_breaker import KEY, PREFIX, T0, Scenario
from test_units import UserErasureTests

DAY = 86400
PAYLOAD = {"platform": "fake", "title": "T", "payload": {}, "preview": {}}


class Raced(Scenario):
    """A person signed in a day before T0, so a sign-in now is a change every path can see; a second connection to
    the same file for the sign-in that lands between the check and the transaction."""

    def setUp(self):
        super().setUp()
        self.other = Store(self.database, KEY)
        self.addCleanup(self.other.close)

    def person(self, name: str) -> dict:
        """One Keycloak-backed person with an access token and a scheduled post (a row the erasure would take)."""
        user = self.store.upsert_user(PREFIX + name, name, None, None)
        self.store._exec("UPDATE users SET last_login_at = ? WHERE id = ?", T0 - DAY, user["id"])
        self.store.save_token(f"ic_at_{name:x<43}", "access", "c1", user["id"], ["inkcast"], None, f"f-{name}", 10 * DAY)
        self.store.create_post(group_id="g", user_id=user["id"], platform="fake", title="T", status="scheduled",
                               run_at=T0 + DAY, payload=PAYLOAD)
        return {**self.store.get_user(user["id"]), "kc": name}

    def person_rows(self, user_id: str) -> dict[str, int]:
        with self.store._lock:
            return {table: int(self.store._conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {condition}",
                                                        {"user_id": user_id}).fetchone()["n"])
                    for table, condition in USER_TABLES}

    def sign_in(self, user: dict) -> dict:
        """Keycloak has the person again and let them in: the callback on another replica commits their sign-in."""
        self.kc.gone.discard(user["kc"])
        back = self.other.upsert_user(PREFIX + user["kc"], user["kc"], None, None)
        self.assertEqual(back["id"], user["id"])
        return back

    def sign_in_at(self, store: Store, method: str, user: dict) -> list[dict]:
        """The first call of ``store.<method>`` about this person (the act's first transaction) is preceded by their
        sign-in on the other connection: the moment between the caller's check and its transaction. Returns the
        list the sign-in's row lands in, so a test can see it happened."""
        real, signed_in = getattr(store, method), []

        def wrapped(user_id, *args, **kwargs):
            if user_id == user["id"] and not signed_in:
                signed_in.append(self.sign_in(user))
            return real(user_id, *args, **kwargs)
        setattr(store, method, wrapped)
        self.addCleanup(delattr, store, method)
        return signed_in

    def assert_intact(self, user: dict, before: dict[str, int], signed_in: list[dict]) -> None:
        self.assertEqual(len(signed_in), 1, "the sign-in landed between the check and the transaction")
        self.assertEqual(self.person_rows(user["id"]), before, "no row of theirs deleted")
        row = self.row(user)
        self.assertEqual(row["last_login_at"], signed_in[0]["last_login_at"], "the sign-in stands")
        self.assertEqual((row["disabled_at"], row["deleted_at"], row["held_at"]), (None, None, None))
        self.assertEqual(self.store.user_erasures(), [], "no audit record")
        self.assertEqual(self.counted("inkcast_identity_users_deleted_total"), {}, "nothing counted")

    def assert_erased_once(self, user: dict, via: str) -> None:
        self.assertEqual(set(self.person_rows(user["id"]).values()), {0}, "every row gone")
        self.assertEqual(len(self.store.user_erasures()), 1, "one audit record")
        self.assertEqual(self.counted("inkcast_identity_users_deleted_total"), {via: 1}, "counted once")


class TheStore(Raced):
    """``erase_user`` with the row as read: nothing unless it still stands."""

    PAYLOAD, seed, rows = UserErasureTests.PAYLOAD, UserErasureTests.seed, staticmethod(UserErasureTests.rows)

    def test_a_sign_in_since_the_row_was_read_erases_nothing(self):
        ada, _ = self.seed(self.store, "ada")
        self.store._exec("UPDATE users SET last_login_at = ? WHERE id = ?", T0 - DAY, ada)
        as_read = self.store.get_user(ada)
        before = self.person_rows(ada)
        back = self.other.upsert_user("oidc:https://kc/realms/i#ada", "ada", None, None)       # another replica's sign-in
        self.assertEqual(back["id"], ada)
        counts = self.store.erase_user(ada, as_read=as_read)
        self.assertEqual(set(counts.values()), {0}, "nothing erased: the row is not as read")
        self.assertTrue(all(type(n) is int for n in counts.values()))
        self.assertEqual(self.person_rows(ada), before)
        self.assertEqual(self.store.user_erasures(), [])
        counts = self.store.erase_user(ada, as_read=self.store.get_user(ada))                 # read again: as they stand now
        self.assertTrue(all(n > 0 for n in counts.values()), counts)
        self.assertEqual(set(self.person_rows(ada).values()), {0})
        self.assertEqual(len(self.store.user_erasures()), 1)

    def test_a_row_marked_present_cut_off_or_gone_since_is_not_as_read_either(self):
        for change in ("present", "cut off", "gone"):
            with self.subTest(change=change):
                self.setUp()
                ada, _ = self.seed(self.store, "ada")
                self.store.mark_user_deleted(ada, T0)
                as_read = self.store.get_user(ada)
                if change == "present":
                    self.other.mark_user_present(ada)                   # a reconcile elsewhere read Keycloak has them
                elif change == "cut off":
                    self.other.disable_user(ada)                        # a pass elsewhere cut them off meanwhile
                else:
                    self.assertGreater(sum(self.other.erase_user(ada).values()), 0)      # erased elsewhere already
                self.assertEqual(set(self.store.erase_user(ada, as_read=as_read).values()), {0})
                self.assertEqual(len(self.store.user_erasures()), 1 if change == "gone" else 0)
                if change != "gone":
                    self.assertEqual(self.person_rows(ada)["users"], 1, "kept")

    def test_unchanged_the_erasure_happens_once_and_without_a_row_as_before(self):
        ada, _ = self.seed(self.store, "ada")
        bob, _ = self.seed(self.store, "bob")
        as_read = self.store.get_user(ada)
        counts = self.store.erase_user(ada, as_read=as_read)
        self.assertTrue(all(n > 0 for n in counts.values()), counts)
        self.assertEqual(set(self.store.erase_user(ada, as_read=as_read).values()), {0}, "gone: not as read, nothing")
        self.assertEqual(len(self.store.user_erasures()), 1, "once")
        self.assertGreater(sum(self.store.erase_user(bob).values()), 0, "no row: erased as they stand (reapply_erasures)")
        self.assertEqual(len(self.store.user_erasures()), 2)


class TheEventsPass(Raced):
    """``tick``: a DELETE_ACCOUNT event, Keycloak's 404, then the erasure."""

    def deleted_own_account(self, user: dict) -> None:
        self.kc.gone.add(user["kc"])
        self.kc.events.append({"time": int(self.now * 1000) + 1000, "type": "DELETE_ACCOUNT", "userId": user["kc"]})

    def test_a_sign_in_between_the_404_and_the_erasure_leaves_the_person_intact(self):
        ada, bob = self.person("kc-ada"), self.person("kc-bob")
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["erased"], 0, "the cursors are set")
        self.deleted_own_account(ada)
        before = self.person_rows(ada["id"])
        signed_in = self.sign_in_at(self.store, "erase_user", ada)
        done = self.tick(worker, T0 + 2)
        self.assertEqual(done["erased"], 0)
        self.assert_intact(ada, before, signed_in)
        self.assertIsNotNone(self.store.get_token(f"ic_at_{ada['kc']:x<43}", "access"), "her token lives")
        self.assertEqual(self.person_rows(bob["id"])["users"], 1)

    def test_unchanged_the_event_erases_once(self):
        ada = self.person("kc-ada")
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["erased"], 0)
        self.deleted_own_account(ada)
        self.assertEqual(self.tick(worker, T0 + 2)["erased"], 1)
        self.assert_erased_once(ada, "event")
        self.assertEqual(self.tick(worker, T0 + 32)["erased"], 0, "the look-back reads the event again: nothing left")
        self.assert_erased_once(ada, "event")


class TheReconcilesOwnDeletion(Raced):
    """``_reconcile``: a 404 with a DELETE_ACCOUNT event the events pass missed, erased as the pass would have."""

    def missed_event(self, user: dict) -> None:
        self.store.set_sync_state(USER_EVENTS_CURSOR, str(int(self.now * 1000) + 200_000))     # a replica ran fast
        self.kc.gone.add(user["kc"])
        self.kc.events.append({"time": int(self.now * 1000), "type": "DELETE_ACCOUNT", "userId": user["kc"]})

    def test_a_sign_in_between_the_reads_and_the_erasure_leaves_the_person_intact(self):
        ada = self.person("kc-ada")
        self.person("kc-bob")
        worker = self.sync(30)
        self.missed_event(ada)
        before = self.person_rows(ada["id"])
        signed_in = self.sign_in_at(self.store, "erase_user", ada)
        done = self.reconcile(worker)
        self.assertEqual((done["disabled"], done["erased"]), (0, 0))
        self.assert_intact(ada, before, signed_in)

    def test_unchanged_the_reconcile_erases_once(self):
        ada = self.person("kc-ada")
        worker = self.sync(30)
        self.missed_event(ada)
        self.assertEqual(self.reconcile(worker)["erased"], 1)
        self.assert_erased_once(ada, "reconcile")
        self.assertEqual(self.reconcile(worker, T0 + 600)["erased"], 0)
        self.assert_erased_once(ada, "reconcile")


class TheGracePeriod(Raced):
    """``_grace_over``: cut off N days ago, still gone, then the erasure."""

    def test_a_sign_in_between_the_grace_check_and_the_erasure_leaves_the_person_intact(self):
        ada = self.person("kc-ada")
        self.person("kc-bob")
        worker = self.sync(30)
        self.kc.gone.add(ada["kc"])                                 # an administrator deleted her: no event
        self.assertEqual(self.reconcile(worker)["disabled"], 1)
        self.assert_cut_off(ada, deleted_at=T0)
        before = self.person_rows(ada["id"])
        signed_in = self.sign_in_at(self.store, "erase_user", ada)
        done = self.reconcile(worker, T0 + 30 * DAY)
        self.assertEqual((done["disabled"], done["erased"]), (0, 0))
        self.assert_intact(ada, before, signed_in)

    def test_with_zero_days_the_cut_off_and_the_erasure_are_one_reconcile_and_the_sign_in_still_spares_her(self):
        ada = self.person("kc-ada")
        self.person("kc-bob")
        worker = self.sync(0)
        self.kc.gone.add(ada["kc"])
        signed_in = self.sign_in_at(self.store, "erase_user", ada)
        done = self.reconcile(worker)
        self.assertEqual((done["disabled"], done["erased"]), (1, 0), "cut off on the 404 (the sign-in restores that); not erased")
        self.assertEqual(len(signed_in), 1)
        row = self.row(ada)
        self.assertIsNotNone(row, "kept")
        self.assertEqual((row["disabled_at"], row["deleted_at"]), (None, None), "the sign-in cleared the cut-off")
        self.assertEqual(self.person_rows(ada["id"])["posts"], 1)
        self.assertEqual(self.store.user_erasures(), [])
        self.assertEqual(self.counted("inkcast_identity_users_deleted_total"), {})

    def test_unchanged_the_grace_period_erases_once(self):
        ada = self.person("kc-ada")
        worker = self.sync(30)
        self.kc.gone.add(ada["kc"])
        self.reconcile(worker)
        self.assertEqual(self.reconcile(worker, T0 + 30 * DAY)["erased"], 1)
        self.assert_erased_once(ada, "reconcile")
        self.assertEqual(self.reconcile(worker, T0 + 31 * DAY)["erased"], 0)
        self.assert_erased_once(ada, "reconcile")


class TheBreakerConfirmation(Raced):
    """``inkcast identity confirm-deletions``: the pass's ``last_login_at``, the person's row now, then the cut-off
    (and, for the gone, the start of the erasure clock) on the pass's say-so."""

    def trip(self, gone: list[dict], disabled: list[dict] = ()) -> str:
        self.kc.gone.update(u["kc"] for u in gone)
        self.kc.disabled.update(u["kc"] for u in disabled)
        self.reconcile(self.sync(30))
        trips = self.trips()
        self.assertEqual(len(trips), 1)
        return trips[0]["id"]

    def confirm(self, pass_id: str):
        from inkcast.breaker import _confirm
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.assertEqual(_confirm(self.store, pass_id, "on-call", T0 + 3600), 0)
        return logs.records

    def test_a_sign_in_between_the_check_and_the_cut_off_spares_a_gone_person(self):
        people = [self.person(f"kc-p{i}") for i in range(8)]
        gone, kept = people[:5], people[5:]
        pass_id = self.trip(gone)
        ada = gone[0]
        before = self.person_rows(ada["id"])
        signed_in = self.sign_in_at(self.store, "mark_user_deleted", ada)
        records = self.confirm(pass_id)
        self.assert_intact(ada, {**before, "sync_breaker_people": 0}, signed_in)   # a resolved pass keeps no ids
        self.assertIsNotNone(self.store.get_token(f"ic_at_{ada['kc']:x<43}", "access"), "her token lives")
        for user in gone[1:]:
            self.assert_cut_off(user, deleted_at=T0 + 3600)
        for user in kept:
            self.assert_untouched(user)
        cut = [r.user_id for r in records if getattr(r, "event", "") == "identity.user.disabled"]
        self.assertEqual(sorted(cut), sorted(u["id"] for u in gone[1:]), "no cut-off line for her")
        (confirmed,) = [r for r in records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"]
        self.assertEqual((confirmed.applied, confirmed.skipped), (4, 1), "the audit line counts her as spared")
        self.assertEqual(self.store.breaker_trip_people(pass_id), [])

    def test_a_sign_in_between_the_check_and_the_cut_off_spares_a_disabled_person(self):
        people = [self.person(f"kc-p{i}") for i in range(8)]
        disabled = people[:5]
        pats = {u["id"]: self.store.create_api_token(u["id"], "ci", ["posts:read"], None)[0] for u in disabled}
        pass_id = self.trip([], disabled)
        ada = disabled[0]
        self.assertIsNotNone(self.row(ada)["held_at"], "held")
        before = self.person_rows(ada["id"])
        signed_in = self.sign_in_at(self.store, "disable_user", ada)   # Keycloak enabled her again and let her in
        records = self.confirm(pass_id)
        self.assert_intact(ada, {**before, "sync_breaker_people": 0}, signed_in)
        self.assertIsNotNone(self.store.find_api_token(pats[ada["id"]]), "her PAT works")
        for user in disabled[1:]:
            self.assertIsNotNone(self.row(user)["disabled_at"])
            self.assertIsNone(self.store.find_api_token(pats[user["id"]]), "revoked")
        (confirmed,) = [r for r in records if getattr(r, "event", "") == "identity.sync.breaker.confirmed"]
        self.assertEqual((confirmed.applied, confirmed.skipped), (4, 1))

    def test_unchanged_the_confirmation_cuts_off_and_the_next_reconcile_erases_once(self):
        people = [self.person(f"kc-p{i}") for i in range(8)]
        gone = people[:5]
        pass_id = self.trip(gone)
        self.confirm(pass_id)
        for user in gone:
            self.assert_cut_off(user, deleted_at=T0 + 3600)
        worker = self.sync(0)
        self.assertEqual(self.reconcile(worker, T0 + 3600 + 600)["erased"], 5)
        self.assertEqual(len(self.store.user_erasures()), 5)
        self.assertEqual(self.counted("inkcast_identity_users_deleted_total"), {"reconcile": 5})
        self.assertEqual(self.reconcile(worker, T0 + 3600 + 1200)["erased"], 0)
        self.assertEqual(len(self.store.user_erasures()), 5)


class TheReturnVisit(Raced):
    """``/account/return``: the cookie's request, Keycloak's 404 through the sync's read, then the erasure, in the
    IdentityService over the same database file."""

    def app(self):
        specs = resolve("IdentityService")
        ctx = build(self.config(30), specs, transport=httpx2.MockTransport(self.kc))
        self.addCleanup(ctx.store.close)
        self.addCleanup(lambda: asyncio.run(ctx.http.aclose()))
        client = TestClient(compose(specs, ctx), base_url="http://localhost:8787")
        self.addCleanup(client.close)
        return ctx, client

    def armed(self, user: dict) -> str:
        """The person pressed Continue: the request is armed for the visit back; the cookie that matches it."""
        req = self.store.open_deletion_link(self.store.create_deletion_request(user["id"], 900))
        return self.store.arm_deletion_return(req["id"], user["kc"], 1800)

    def test_a_sign_in_between_the_404_and_the_erasure_leaves_the_person_intact(self):
        ada = self.person("kc-ada")
        cookie = self.armed(ada)
        ctx, client = self.app()
        self.kc.gone.add(ada["kc"])                                 # Keycloak deleted the account
        before = self.person_rows(ada["id"])
        signed_in = self.sign_in_at(ctx.store, "erase_user", ada)
        with client:
            page = client.get("/account/return", cookies={"inkcast_deletion": cookie})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(signed_in), 1)
        self.assertEqual(self.person_rows(ada["id"]), before, "no row of theirs deleted")
        self.assertEqual(ctx.store.user_erasures(), [], "no audit record")
        self.assertNotIn("inkcast_identity_users_deleted_total{", ctx.metrics.render(), "nothing counted")
        self.assertNotIn("Your account is deleted", page.text, "not told their data is erased")
        self.assertIsNotNone(self.store.deletion_request_by_cookie(cookie), "the record stays for a later visit")

    def test_unchanged_the_visit_erases_once(self):
        ada = self.person("kc-ada")
        cookie = self.armed(ada)
        ctx, client = self.app()
        self.kc.gone.add(ada["kc"])
        with client:
            page = client.get("/account/return", cookies={"inkcast_deletion": cookie})
            again = client.get("/account/return", cookies={"inkcast_deletion": cookie})
        self.assertIn("Your account is deleted", page.text)
        self.assertEqual(set(self.person_rows(ada["id"]).values()), {0})
        self.assertEqual(len(ctx.store.user_erasures()), 1)
        self.assertIn('inkcast_identity_users_deleted_total{via="return"} 1', ctx.metrics.render())
        self.assertIsNone(self.store.deletion_request_by_cookie(cookie))
        self.assertNotIn("Your account is deleted", again.text, "the record is gone: the neutral page")
        self.assertEqual(len(ctx.store.user_erasures()), 1)


del UserErasureTests                       # helpers only; its tests belong to test_units

if __name__ == "__main__":
    unittest.main()
