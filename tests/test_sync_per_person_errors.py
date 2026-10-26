# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #103: one person whose Keycloak lookup always fails must not block the sync for everyone after them.

The reconcile walks people in id order; the event passes walk Keycloak's events newest first. Before this fix a
lookup that errored (a 500 for one user, a malformed id) raised out of ``tick``: nobody after that person was cut
off or erased, everyone before them was re-read at the next tick (the reconcile was not recorded as done, so it
ran again 30 s later, not 10 minutes), and a DELETE_ACCOUNT event newer than theirs blocked the erasures behind it.

Now an error Keycloak answers about one person (``PersonError``: a status on ``users/{id}`` or their events that
the sync cannot act on) is counted as ``inkcast_identity_sync_errors_total{stage="person"}``, logged with the
Inkcast user id and fixed fields only, and the pass goes on. What must still hold: the DELETE_ACCOUNT cursor never
passes the event of a person whose erasure did not happen (it is read again every pass until it does), the
reconcile keeps its schedule (the failed person is re-read at the next one, not in a tight loop), and the pass as a
whole still fails as before when the sync, not a person, is the problem: a reconcile that could act on nobody (so
``InkcastIdentitySyncFailing`` keeps its meaning for a realm-wide 403 or 500), the service account refused, a
transport error on the way to Keycloak (a connection refused: never one person's, #152), or no answer to a pass's
budget of reads about people with none answered (#121 made a read about one person with no answer that person's
alone, since one user whose read hangs must not block everyone after them; #152 gave a pass a budget of three such
reads, so that Keycloak being down costs it the budget, not a timeout per person; ``test_sync_person_timeouts``,
``test_sync_timeout_budget``)."""

import asyncio
import unittest

import httpx2

from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.secrets import SecretValue
from inkcast.services.identity.keycloak_sync import USER_EVENTS_CURSOR, KeycloakSync, SyncError

ISSUER = "https://kc.example/realms/inkcast"
PREFIX = f"oidc:{ISSUER}#"
DAY = 86400
T0 = 1_700_000_000.0
ERRORS = ("inkcast_identity_sync_errors_total", (("stage", "person"),))


class Keycloak:
    """A Keycloak admin API over ``httpx2.MockTransport``: who is gone (404 ``User not found``), who is disabled, who
    answers an error status (``failing``: Keycloak id -> status, for ``users/{id}``; ``events_failing`` likewise for
    the DELETE_ACCOUNT events about them), who is unreachable (``unreachable``: a connection error), whose read finds the service account revoked (``revoked_at``:
    401, and the token endpoint refuses from then on), the USER admin events and the DELETE_ACCOUNT user events
    (filtered by type and user, newest first), and which people the sync read."""

    def __init__(self):
        self.gone: set[str] = set()
        self.disabled: set[str] = set()
        self.failing: dict[str, int] = {}
        self.events_failing: dict[str, int] = {}
        self.unreachable: set[str] = set()
        self.revoked_at: "str | None" = None
        self.refuse_token = False
        self.events: list[dict] = []
        self.admin_events: list[dict] = []
        self.read: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path, q = request.url.path, request.url.params
        if path.endswith("/token"):
            if self.refuse_token:
                return httpx2.Response(401, json={"error": "unauthorized_client"})
            return httpx2.Response(200, json={"access_token": "t", "expires_in": 300})
        if path.endswith("/admin-events"):
            return httpx2.Response(200, json=sorted(self.admin_events, key=lambda e: -e["time"]))
        if path.endswith("/events"):
            if q.get("user") in self.events_failing:
                return httpx2.Response(self.events_failing[q.get("user")], json={"error": "unknown_error"})
            found = [e for e in self.events if e["type"] in q.get_list("type") and q.get("user") in (None, e["userId"])]
            return httpx2.Response(200, json=sorted(found, key=lambda e: -e["time"])[: int(q.get("max", "100"))])
        keycloak_id = path.rsplit("/", 1)[1]
        self.read.append(keycloak_id)
        if keycloak_id in self.unreachable:
            raise httpx2.ConnectError("connection refused", request=request)
        if keycloak_id == self.revoked_at:
            self.refuse_token = True
            return httpx2.Response(401, json={"error": "HTTP 401 Unauthorized"})
        if keycloak_id in self.failing:
            return httpx2.Response(self.failing[keycloak_id], json={"error": "unknown_error"})
        if keycloak_id in self.gone:
            return httpx2.Response(404, json={"error": "User not found"})
        return httpx2.Response(200, json={"id": keycloak_id, "enabled": keycloak_id not in self.disabled})


class Scenario(unittest.TestCase):
    """One store, one mock Keycloak, a clock the test moves. ``tick`` records a pass that failed as a whole instead
    of raising, so a test says which passes may fail (``pass_errors``)."""

    def setUp(self):
        self.kc = Keycloak()
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.now = T0
        self.counted: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.pass_errors: list[Exception] = []
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.kc))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))

    def people(self, *names: str) -> list[dict]:
        """Inkcast users for these Keycloak ids, in the order the reconcile walks them (by Inkcast id); each row
        carries its Keycloak id as ``kc``."""
        rows = []
        for name in names:
            user = self.store.upsert_user(PREFIX + name, name, None, None)
            self.store.save_token(f"ic_at_{name:x<43}", "access", "c1", user["id"], ["inkcast"], None, f"f-{name}", 10 * DAY)
            rows.append({**user, "kc": name})
        return sorted(rows, key=lambda u: u["id"])

    def sync(self, days: "int | None" = 30, reconcile_seconds: float = 600.0) -> KeycloakSync:
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="oidc",
                     oidc_issuer=ISSUER, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                     oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync",
                     keycloak_sync_secret=SecretValue("sync-secret"), keycloak_erase_after_days=days,
                     keycloak_reconcile_seconds=reconcile_seconds)
        cfg.validate()
        scenario = self

        class Metrics:
            def inc(self, name, amount=1, **labels):
                scenario.counted.append((name, tuple(sorted(labels.items()))))

        worker = KeycloakSync(cfg, self.store, self.http, Metrics(), clock=lambda: self.now)
        worker.last_reconcile = self.now                            # a pass is the events path alone until reconcile()
        return worker

    def tick(self, worker: KeycloakSync, at: "float | None" = None) -> "dict | None":
        if at is not None:
            self.now = at
        try:
            return asyncio.run(worker.tick())
        except Exception as exc:                                    # a pass that failed as a whole
            self.pass_errors.append(exc)
            return None

    def reconcile(self, worker: KeycloakSync, at: "float | None" = None) -> "dict | None":
        worker.last_reconcile = 0
        return self.tick(worker, at)

    def row(self, user: dict) -> "dict | None":
        return self.store.get_user(user["id"])

    def errors_counted(self) -> int:
        return self.counted.count(ERRORS)

    def assert_untouched(self, user: dict) -> None:
        row = self.row(user)
        self.assertIsNotNone(row, f"{user['kc']} keeps their row")
        self.assertIsNone(row["disabled_at"], f"{user['kc']} is not cut off")
        self.assertIsNotNone(self.store.get_token(f"ic_at_{user['kc']:x<43}", "access"), f"{user['kc']}'s token lives")

    def assert_cut_off(self, user: dict) -> None:
        row = self.row(user)
        self.assertIsNotNone(row, f"{user['kc']} keeps their row")
        self.assertIsNotNone(row["disabled_at"], f"{user['kc']} is cut off")
        self.assertIsNone(self.store.get_token(f"ic_at_{user['kc']:x<43}", "access"), f"{user['kc']}'s token is dead")


class TheReconcileGoesOn(Scenario):
    def test_people_after_a_person_who_answers_500_are_still_cut_off_and_erased_when_the_policy_says(self):
        bad, gone, disabled = self.people("ada", "bob", "carol")   # bad sorts first: on main nobody after is reached
        self.kc.failing[bad["kc"]] = 500
        self.kc.gone.add(gone["kc"])                                # an administrator deleted them: no event
        self.kc.disabled.add(disabled["kc"])
        worker = self.sync(30)
        done = self.reconcile(worker)
        self.assertEqual(self.pass_errors, [], "one person's 500 failed the whole pass")
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (3, 2, 0))
        self.assert_cut_off(gone)
        self.assertEqual(self.row(gone)["deleted_at"], T0, "the grace period runs from this reconcile")
        self.assert_cut_off(disabled)
        self.assert_untouched(bad)                                  # Keycloak said nothing about them: nothing done
        self.assertEqual(self.reconcile(worker, T0 + 29 * DAY)["erased"], 0, "inside the grace period")
        self.assertEqual(self.reconcile(worker, T0 + 30 * DAY)["erased"], 1, "30 days later, still gone: erased")
        self.assertIsNone(self.row(gone), "erased when the policy says, past the person who keeps failing")
        self.assert_cut_off(disabled)
        self.assert_untouched(bad)
        self.assertEqual(self.pass_errors, [])

    def test_the_failure_is_counted_and_logged_with_the_inkcast_id_and_fixed_fields_only(self):
        # Keycloak ids with non-hex letters: the Inkcast id (usr_<hex>) can't contain them by chance, as "ada" could.
        bad, other = self.people("kc-ada", "kc-bob")
        self.kc.failing[bad["kc"]] = 500
        worker = self.sync(30)
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            self.reconcile(worker)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(self.errors_counted(), 1, 'inkcast_identity_sync_errors_total{stage="person"} once per failure')
        self.assertEqual(self.counted.count(("inkcast_identity_sync_total", (("result", "error"),))), 0)
        failed = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.person.failed"]
        self.assertEqual(len(failed), 1, "one line per person per pass")
        record = failed[0]
        self.assertEqual(record.user_id, bad["id"])
        self.assertEqual(record.status, 500)
        self.assertEqual((record.call, record.via), ("user", "reconcile"))
        self.assertNotIn(bad["kc"], record.getMessage(), "the Keycloak id is not logged")
        self.assertNotIn("unknown_error", record.getMessage(), "nothing from Keycloak's answer is logged")
        self.assertEqual(self.reconcile(worker)["checked"], 2)
        self.assertEqual(self.errors_counted(), 2, "counted again at the next reconcile: the alert sees it keep failing")
        self.assert_untouched(other)

    def test_the_next_reconcile_runs_on_schedule_not_at_the_next_tick(self):
        bad, other = self.people("ada", "bob")
        self.kc.failing[bad["kc"]] = 500
        worker = self.sync(30, reconcile_seconds=600)
        self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(worker.last_reconcile, T0, "a reconcile with one failure is still a reconcile that ran")
        self.assertEqual(worker.last_success, T0)
        reads = len(self.kc.read)
        for seconds in (30, 60, 599):                              # the sync interval passes: the events path alone
            self.tick(worker, T0 + seconds)
        self.assertEqual(len(self.kc.read), reads, "the reconcile ran again before it was due (a tight retry loop)")
        self.assertEqual(self.errors_counted(), 1)
        self.tick(worker, T0 + 600)                                 # due: everyone is re-read, the failed person too
        self.assertEqual(self.kc.read[reads:], [bad["kc"], other["kc"]])
        self.assertEqual(self.errors_counted(), 2)
        self.assertEqual(self.pass_errors, [])

    def test_a_person_found_gone_whose_events_cannot_be_read_is_cut_off_now_and_erased_after_the_grace_period(self):
        gone, other = self.people("ada", "bob")
        self.kc.gone.add(gone["kc"])                                # the 404 is certain; the DELETE_ACCOUNT read is not
        self.kc.events_failing[gone["kc"]] = 500
        worker = self.sync(30)
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual((done["disabled"], done["erased"]), (1, 0), "cut off at once: nothing is lost by it")
        self.assert_cut_off(gone)
        self.assertEqual(self.row(gone)["deleted_at"], T0, "the grace period runs from now")
        self.assert_untouched(other)
        record = [r for r in logs.records if getattr(r, "event", "") == "identity.sync.person.failed"][0]
        self.assertEqual((record.user_id, record.call, record.status), (gone["id"], "events", 500))
        self.assertEqual(self.errors_counted(), 1)
        self.assertEqual(self.reconcile(worker, T0 + 29 * DAY)["erased"], 0)
        self.assertEqual(self.reconcile(worker, T0 + 30 * DAY)["erased"], 1, "gone for the grace period: erased")
        self.assertIsNone(self.row(gone))
        self.assertEqual(self.errors_counted(), 3)
        # Or at once, when their events can be read again and hold a DELETE_ACCOUNT the events pass missed.
        late = self.people("carol")[0]
        self.kc.gone.add(late["kc"])
        self.kc.events.append({"time": int(self.now * 1000), "type": "DELETE_ACCOUNT", "realmId": "inkcast",
                               "userId": late["kc"]})
        self.store.set_sync_state(USER_EVENTS_CURSOR, str(int(self.now * 1000) + 120_000))   # a replica ran fast
        self.kc.events_failing[late["kc"]] = 503
        self.assertEqual(self.reconcile(worker, T0 + 31 * DAY)["disabled"], 1)
        self.assertIsNotNone(self.row(late))
        del self.kc.events_failing[late["kc"]]
        self.assertEqual(self.reconcile(worker, T0 + 31 * DAY + 600)["erased"], 1, "her event, read at last")
        self.assertIsNone(self.row(late))
        self.assertEqual(self.pass_errors, [])


class TheEventPassesGoOn(Scenario):
    def event(self, keycloak_id: str, offset_ms: int) -> dict:
        return {"time": int(self.now * 1000) + offset_ms, "type": "DELETE_ACCOUNT", "realmId": "inkcast",
                "userId": keycloak_id}

    def test_an_erasure_blocked_by_a_newer_event_that_fails_happens_and_the_failed_one_is_retried_not_lost(self):
        by_kc = {u["kc"]: u for u in self.people("ada", "bob", "carol")}
        ada, bob, carol = by_kc["ada"], by_kc["bob"], by_kc["carol"]
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["erased"], 0, "the cursor is set")
        self.kc.gone.update({"bob", "carol"})
        self.kc.failing["ada"] = 500                                # she deleted her account too, but her lookup fails
        self.kc.events += [self.event("bob", 1_000), self.event("ada", 2_000), self.event("carol", 3_000)]
        ada_at, carol_at = self.kc.events[1]["time"], self.kc.events[2]["time"]

        first = self.tick(worker, T0 + 4)
        self.assertEqual(self.pass_errors, [], "one person's 500 failed the whole pass")
        self.assertEqual(first["erased"], 2, "bob's older event was blocked behind ada's")
        self.assertIsNone(self.row(bob))
        self.assertIsNone(self.row(carol))
        self.assertEqual(int(self.store.get_sync_state(USER_EVENTS_CURSOR)), ada_at,
                         "the cursor stays at the event of the person whose erasure did not happen")
        self.assertEqual(self.errors_counted(), 1)

        second = self.tick(worker, T0 + 34)                         # still failing: read again, nothing else changes
        self.assertEqual((second["erased"], self.errors_counted()), (0, 2))
        self.assertEqual(int(self.store.get_sync_state(USER_EVENTS_CURSOR)), ada_at)
        self.assertIsNotNone(self.row(ada), "nothing is done to a person Keycloak answered an error for")

        del self.kc.failing["ada"]                                  # Keycloak answers about her again: she is gone
        self.kc.gone.add("ada")
        third = self.tick(worker, T0 + 64)
        self.assertEqual(third["erased"], 1, "the erasure that failed was lost")
        self.assertIsNone(self.row(ada))
        self.assertEqual(int(self.store.get_sync_state(USER_EVENTS_CURSOR)), carol_at, "every event acted on: the cursor moves")
        self.assertEqual([lab for name, lab in self.counted if name == "inkcast_identity_users_deleted_total"],
                         [(("via", "event"),)] * 3, "each erased once, by the events pass, not left to the reconcile")
        self.assertEqual(self.pass_errors, [])

    def test_the_admin_events_pass_goes_on_past_a_person_who_answers_500(self):
        bad, disabled = self.people("ada", "bob")
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursor is set")
        self.kc.failing[bad["kc"]] = 500
        self.kc.disabled.add(disabled["kc"])
        for keycloak_id, offset in ((disabled["kc"], 1_000), (bad["kc"], 2_000)):   # bad's is newer: read first
            self.kc.admin_events.append({"time": int(self.now * 1000) + offset, "resourceType": "USER",
                                         "operationType": "UPDATE", "resourcePath": f"users/{keycloak_id}"})
        done = self.tick(worker, T0 + 3)
        self.assertEqual(self.pass_errors, [], "one person's 500 failed the whole pass")
        self.assertEqual((done["checked"], done["disabled"]), (2, 1))
        self.assert_cut_off(disabled)
        self.assert_untouched(bad)
        self.assertEqual(self.errors_counted(), 1)


class WhatStillFailsThePass(Scenario):
    """The per-person handling is for an answer about one person. The sync as a whole is still judged as before."""

    def test_a_reconcile_that_can_act_on_nobody_is_a_failed_pass(self):
        people = self.people("ada", "bob", "carol")
        for user in people:                                         # the service account lost view-users: 403 for all
            self.kc.failing[user["kc"]] = 403
        worker = self.sync(30)
        self.assertIsNone(self.reconcile(worker, T0))
        self.assertEqual(len(self.pass_errors), 1)
        self.assertIs(type(self.pass_errors[0]), SyncError, "the sync failed, not a person (PersonError)")
        self.assertEqual(self.errors_counted(), 3, "each person's failure is still counted")
        self.assertEqual(worker.last_reconcile, 0, "not a reconcile that ran: retried at the next tick, as before")
        self.assertEqual(worker.last_success, 0.0)
        for user in people:
            self.assert_untouched(user)

    def test_a_connection_error_still_fails_the_pass_as_a_whole_at_once(self):
        down, still_down, after = self.people("ada", "bob", "carol")
        self.kc.unreachable.update({down["kc"], still_down["kc"]})  # Keycloak went away mid-pass
        worker = self.sync(30)
        self.assertIsNone(self.reconcile(worker, T0))
        self.assertEqual(len(self.pass_errors), 1)
        self.assertIs(type(self.pass_errors[0]), SyncError, "the sync failed, not a person: on the way there (#152)")
        self.assertIsInstance(self.pass_errors[0].__cause__, httpx2.ConnectError)
        self.assertEqual(self.kc.read, [down["kc"]], "nobody else was tried: a connection refused is nobody's")
        self.assertEqual(self.errors_counted(), 0, "not a person's failure")
        self.assertEqual(worker.last_reconcile, 0)
        self.assert_untouched(after)

    def test_the_service_account_refused_mid_pass_still_fails_the_pass_as_a_whole(self):
        first, after = self.people("ada", "bob")
        self.kc.revoked_at = first["kc"]                            # the secret was rotated on one side mid-pass
        worker = self.sync(30)
        self.assertIsNone(self.reconcile(worker, T0))
        self.assertEqual(len(self.pass_errors), 1)
        self.assertIs(type(self.pass_errors[0]), SyncError, "nothing about that person: the service account")
        self.assertEqual(self.kc.read, [first["kc"]], "the new token is refused before a second read")
        self.assertEqual(self.errors_counted(), 0)
        self.assertEqual(worker.last_reconcile, 0)


if __name__ == "__main__":
    unittest.main()
