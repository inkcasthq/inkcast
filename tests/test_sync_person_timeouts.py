# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #121: a read that gets no answer for one person no longer blocks everyone after them.

#103 (PR #115) made an error Keycloak *answers* about one person (a 500 for that user, a malformed id) that person's
alone: counted, logged with ids, and the pass goes on. A read that got no answer at all (a timeout, a connection
refused or reset: ``httpx2.TransportError``) still failed the whole pass, so one user whose ``users/{id}`` read hangs
(an LDAP referral to an unreachable server, past the client's 30 s) stopped the reconcile for everyone after them on
every pass: the #103 flaw for this cause. Now such a read is a person error too (``PersonError`` with status 0 and
the error's name). #121 failed the pass on the second in a row, so that a Keycloak outage cost a pass two timeouts
rather than one per person; #152 replaced that with a budget per pass (``TIMEOUT_BUDGET``, 3: ``test_sync_timeout_budget``),
since two people whose reads hang and who are read next to each other failed every pass. A pass now ends at the
third such read, as a success for what it did, and fails as a whole only when no read about a person answered. A
transport error on the way to Keycloak (a connection refused, the client's pool) is never a person's. What must
still hold: the DELETE_ACCOUNT cursor never passes the event of a person whose erasure did not happen; a person
whose read got no answer is neither gone nor present, so the breaker neither counts them nor acts on them; and no
answer from the token endpoint is the service account's failure, never a person's.

Each test fails on the code before #121, where the first timeout raises out of ``tick``."""

import asyncio
import logging
import unittest

import httpx2
from test_sync_per_person_errors import DAY, T0, USER_EVENTS_CURSOR, Keycloak as _Keycloak, Scenario as _Scenario, SyncError

NO_ANSWER = (httpx2.ReadTimeout, httpx2.ReadError)     # the request reached Keycloak: a person's (#152 shape 6)
DETAIL = "the transport's own words: never logged"


class Keycloak(_Keycloak):
    """The #103 fake, plus reads that get no answer: ``no_answer`` (Keycloak id -> the httpx2 error raised for
    ``users/{id}``), ``events_no_answer`` (likewise for the DELETE_ACCOUNT events about them) and ``token_no_answer``
    (the token endpoint, once ``revoked_at``'s 401 has sent the sync back to it)."""

    def __init__(self):
        super().__init__()
        self.no_answer: dict[str, type[httpx2.TransportError]] = {}
        self.events_no_answer: dict[str, type[httpx2.TransportError]] = {}
        self.token_no_answer: "type[httpx2.TransportError] | None" = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path, q = request.url.path, request.url.params
        if path.endswith("/token"):
            if self.token_no_answer and self.refuse_token:
                raise self.token_no_answer(DETAIL, request=request)
            return super().__call__(request)
        if path.endswith(("/admin-events", "/events")):
            if path.endswith("/events") and q.get("user") in self.events_no_answer:
                raise self.events_no_answer[q.get("user")](DETAIL, request=request)
            return super().__call__(request)
        keycloak_id = path.rsplit("/", 1)[1]
        if keycloak_id in self.no_answer:
            self.read.append(keycloak_id)
            raise self.no_answer[keycloak_id](DETAIL, request=request)
        return super().__call__(request)


class Scenario(_Scenario):
    def setUp(self):
        super().setUp()
        self.kc = Keycloak()
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.kc))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))

    def failed(self, logs) -> list[logging.LogRecord]:
        return [r for r in logs.records if getattr(r, "event", "") == "identity.sync.person.failed"]

    def pass_failures_counted(self) -> int:
        return self.counted.count(("inkcast_identity_sync_total", (("result", "error"),)))

    def cursor(self) -> int:
        return int(self.store.get_sync_state(USER_EVENTS_CURSOR))

    def event(self, keycloak_id: str, offset_ms: int) -> dict:
        return {"time": int(self.now * 1000) + offset_ms, "type": "DELETE_ACCOUNT", "realmId": "inkcast",
                "userId": keycloak_id}

    def assert_untouched(self, user: dict) -> None:
        super().assert_untouched(user)
        row = self.row(user)
        self.assertIsNone(row["deleted_at"], f"{user['kc']}: no grace period runs")
        self.assertIsNone(row.get("held_at"), f"{user['kc']} is not held")


class OnePersonWhoseReadHangs(Scenario):
    def test_the_reconcile_still_cuts_off_and_erases_the_people_after_them_and_counts_the_error(self):
        for error in NO_ANSWER:
            with self.subTest(error=error.__name__):
                self.setUp()
                hangs, gone, disabled = self.people("ada", "bob", "carol")   # hangs sorts first: on main nobody after is reached
                self.kc.no_answer[hangs["kc"]] = error
                self.kc.gone.add(gone["kc"])                                # an administrator deleted them: no event
                self.kc.disabled.add(disabled["kc"])
                worker = self.sync(30)
                with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
                    done = self.reconcile(worker)
                self.assertEqual(self.pass_errors, [], "one person's read with no answer failed the whole pass")
                self.assertEqual((done["checked"], done["disabled"], done["erased"]), (3, 2, 0))
                self.assert_cut_off(gone)
                self.assertEqual(self.row(gone)["deleted_at"], T0, "the grace period runs from this reconcile")
                self.assert_cut_off(disabled)
                self.assert_untouched(hangs)                                # no answer about them: nothing done
                self.assertEqual(self.errors_counted(), 1, 'inkcast_identity_sync_errors_total{stage="person"} once')
                self.assertEqual(self.pass_failures_counted(), 0)
                failed = self.failed(logs)
                self.assertEqual(len(failed), 1, "one line per person per pass")
                record = failed[0]
                self.assertEqual((record.user_id, record.via, record.call), (hangs["id"], "reconcile", "user"))
                self.assertEqual((record.status, record.cause), (0, error.__name__), "no HTTP status: the error's name")
                self.assertNotIn(hangs["kc"], record.getMessage(), "the Keycloak id is not logged")
                self.assertNotIn(DETAIL, record.getMessage(), "nothing from the exception is logged: its name only")
                self.assertEqual(worker.last_reconcile, T0, "a reconcile with one failure is still a reconcile that ran")
                reads = len(self.kc.read)
                self.tick(worker, T0 + 30)                                  # the events path alone: no re-read
                self.assertEqual(len(self.kc.read), reads, "the reconcile ran again before it was due")
                self.assertEqual(self.reconcile(worker, T0 + 30 * DAY)["erased"], 1,
                                 "30 days later, still gone: erased past the person whose read keeps hanging")
                self.assertIsNone(self.row(gone))
                self.assert_cut_off(disabled)
                self.assert_untouched(hangs)
                self.assertEqual((self.errors_counted(), self.pass_errors), (2, []))

    def test_the_admin_events_pass_goes_on_past_them(self):
        hangs, disabled = self.people("ada", "bob")
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursor is set")
        self.kc.no_answer[hangs["kc"]] = httpx2.ReadTimeout
        self.kc.disabled.add(disabled["kc"])
        for keycloak_id, offset in ((disabled["kc"], 1_000), (hangs["kc"], 2_000)):   # hangs' is newer: read first
            self.kc.admin_events.append({"time": int(self.now * 1000) + offset, "resourceType": "USER",
                                         "operationType": "UPDATE", "resourcePath": f"users/{keycloak_id}"})
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.tick(worker, T0 + 3)
        self.assertEqual(self.pass_errors, [], "one person's read with no answer failed the whole pass")
        self.assertEqual((done["checked"], done["disabled"]), (2, 1))
        self.assert_cut_off(disabled)
        self.assert_untouched(hangs)
        self.assertEqual(self.errors_counted(), 1)
        record = self.failed(logs)[0]
        self.assertEqual((record.user_id, record.via, record.call, record.status, record.cause),
                         (hangs["id"], "event", "user", 0, "ReadTimeout"))

    def test_a_person_found_gone_whose_events_read_hangs_is_cut_off_now_as_for_a_500(self):
        gone, other = self.people("ada", "bob")
        self.kc.gone.add(gone["kc"])                                # the 404 is certain; the DELETE_ACCOUNT read is not
        self.kc.events_no_answer[gone["kc"]] = httpx2.ReadTimeout
        worker = self.sync(30)
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual((done["disabled"], done["erased"]), (1, 0), "cut off at once: nothing is lost by it (#103)")
        self.assert_cut_off(gone)
        self.assertEqual(self.row(gone)["deleted_at"], T0, "the grace period runs from now")
        self.assert_untouched(other)
        record = self.failed(logs)[0]
        self.assertEqual((record.user_id, record.call, record.status, record.cause), (gone["id"], "events", 0, "ReadTimeout"))
        self.assertEqual(self.errors_counted(), 1)
        self.assertEqual(self.reconcile(worker, T0 + 30 * DAY)["erased"], 1, "gone for the grace period: erased")
        self.assertIsNone(self.row(gone))
        self.assertEqual((self.errors_counted(), self.pass_errors), (2, []))


class TwoInARow(Scenario):
    """#121 failed the pass on the second read with no answer in a row, so two people whose reads hang and who are
    read next to each other failed every pass (#152). Now each such read is that person's alone, next to each other
    or not, up to the pass's budget (``test_sync_timeout_budget``)."""

    def test_no_answer_to_two_reads_in_a_row_no_longer_fails_the_pass_and_the_person_after_is_tried(self):
        first, second, after = self.people("ada", "bob", "carol")
        self.kc.no_answer[first["kc"]] = httpx2.ReadTimeout         # two people whose reads hang, next to each other
        self.kc.no_answer[second["kc"]] = httpx2.ReadTimeout
        self.kc.gone.add(after["kc"])
        worker = self.sync(30)
        done = self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [], "the second read with no answer in a row failed the pass (#121)")
        self.assertEqual(self.kc.read, [first["kc"], second["kc"], after["kc"]], "carol was tried: two is under the budget")
        self.assertEqual((done["checked"], done["disabled"]), (3, 1))
        self.assert_cut_off(after)
        self.assertEqual(self.errors_counted(), 2, "each is that person's failure; neither is the sync's")
        self.assertEqual((worker.last_reconcile, worker.last_success), (T0, T0), "a reconcile that ran")
        self.assert_untouched(first)
        self.assert_untouched(second)

    def test_each_is_that_persons_alone_and_each_pass_starts_at_nought(self):
        hangs, fine, hangs_too, gone = self.people("ada", "bob", "carol", "dave")
        self.kc.no_answer[hangs["kc"]] = httpx2.ReadTimeout
        self.kc.no_answer[hangs_too["kc"]] = httpx2.ReadError
        self.kc.gone.add(gone["kc"])
        worker = self.sync(30)
        done = self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [], "two reads with no answer: each is that person's alone")
        self.assertEqual((done["checked"], done["disabled"]), (4, 1))
        self.assert_cut_off(gone)
        self.assert_untouched(hangs)
        self.assert_untouched(hangs_too)
        self.assertEqual(self.errors_counted(), 2)
        self.assertEqual(self.reconcile(worker, T0 + 600)["checked"], 4, "the next pass: the budget starts at nought")
        self.assertEqual((self.errors_counted(), self.pass_errors), (4, []))

    def test_no_answer_from_the_token_endpoint_is_the_service_accounts_failure_not_a_persons(self):
        first, after = self.people("ada", "bob")
        self.kc.revoked_at = first["kc"]                            # a 401 sends the sync back to the token endpoint...
        self.kc.token_no_answer = httpx2.ConnectError               # ...which has gone away
        worker = self.sync(30)
        self.assertIsNone(self.reconcile(worker, T0))
        self.assertEqual(len(self.pass_errors), 1)
        self.assertIs(type(self.pass_errors[0]), SyncError, "nothing about that person: the service account")
        self.assertIsInstance(self.pass_errors[0].__cause__, httpx2.ConnectError)
        self.assertEqual(self.kc.read, [first["kc"]])
        self.assertEqual(self.errors_counted(), 0, "not a person's failure")
        self.assertEqual(worker.last_reconcile, 0)
        self.assert_untouched(after)


class TheBreaker(Scenario):
    """A person whose read got no answer is neither gone nor present: not a fifth gone that trips the breaker, not one
    of the people read that the share is of, and never acted on, held or released."""

    def test_a_person_whose_read_hangs_is_not_counted_as_gone(self):
        people = self.people(*(f"p{i:02d}" for i in range(20)))
        hangs, gone, kept = people[0], people[1:5], people[5:]      # 4 gone of 19 read: under the minimum of 5
        self.kc.no_answer[hangs["kc"]] = httpx2.ReadTimeout
        self.kc.gone.update(u["kc"] for u in gone)
        worker = self.sync(30)
        done = self.reconcile(worker)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (20, 4, 0))
        self.assertEqual(self.store.breaker_trips(), [], "no trip: the person with no answer is not a fifth gone")
        for user in gone:
            self.assert_cut_off(user)
            self.assertEqual(self.row(user)["deleted_at"], T0)
        for user in kept:
            self.assert_untouched(user)
        self.assert_untouched(hangs)
        self.assertEqual(self.errors_counted(), 1)

    def test_nor_among_the_people_read_and_an_open_breaker_neither_holds_nor_releases_them(self):
        people = self.people(*(f"p{i:02d}" for i in range(50)))
        hangs, gone = people[0], people[1:6]                        # 5 gone of 49 read: above 10 %; of 50 it would not be
        self.kc.no_answer[hangs["kc"]] = httpx2.ReadTimeout
        self.kc.gone.update(u["kc"] for u in gone)
        worker = self.sync(30)
        done = self.reconcile(worker)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (50, 0, 0), "the breaker tripped: held")
        trips = self.store.breaker_trips()
        self.assertEqual(len(trips), 1)
        self.assertEqual((trips[0]["gone"], trips[0]["read"]), (5, 49))
        self.assertNotIn(hangs["id"], [p["user_id"] for p in self.store.breaker_trip_people(trips[0]["id"])])
        for user in gone:
            self.assert_untouched(user)
        self.assert_untouched(hangs)
        self.assertEqual(self.reconcile(worker, T0 + 600)["disabled"], 0, "still open: still held")
        self.assert_untouched(hangs)
        self.assertEqual(len(self.store.breaker_trips()), 1)
        self.assertEqual((self.errors_counted(), self.pass_errors), (2, []))


class TheDeleteAccountCursor(Scenario):
    def test_the_cursor_never_passes_the_event_of_a_person_whose_read_hangs(self):
        by_kc = {u["kc"]: u for u in self.people("ada", "bob", "carol")}
        ada, bob, carol = by_kc["ada"], by_kc["bob"], by_kc["carol"]
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["erased"], 0, "the cursor is set")
        self.kc.gone.update({"bob", "carol"})
        self.kc.no_answer["ada"] = httpx2.ReadTimeout               # she deleted her account too, but her read hangs
        self.kc.events += [self.event("bob", 1_000), self.event("ada", 2_000), self.event("carol", 3_000)]
        ada_at, carol_at = self.kc.events[1]["time"], self.kc.events[2]["time"]

        first = self.tick(worker, T0 + 4)
        self.assertEqual(self.pass_errors, [], "one person's read with no answer failed the whole pass")
        self.assertEqual(first["erased"], 2, "bob's older event was blocked behind ada's")
        self.assertIsNone(self.row(bob))
        self.assertIsNone(self.row(carol))
        self.assertEqual(self.cursor(), ada_at, "the cursor stays at the event of the person whose erasure did not happen")
        self.assertEqual(self.errors_counted(), 1)

        second = self.tick(worker, T0 + 34)                         # still hanging: read again, nothing else changes
        self.assertEqual((second["erased"], self.errors_counted()), (0, 2))
        self.assertEqual(self.cursor(), ada_at)
        self.assertIsNotNone(self.row(ada), "nothing is done to a person whose read got no answer")

        del self.kc.no_answer["ada"]                                # Keycloak answers about her again: she is gone
        self.kc.gone.add("ada")
        third = self.tick(worker, T0 + 64)
        self.assertEqual(third["erased"], 1, "the erasure that failed was lost")
        self.assertIsNone(self.row(ada))
        self.assertEqual(self.cursor(), carol_at, "every event acted on: the cursor moves")
        self.assertEqual([lab for name, lab in self.counted if name == "inkcast_identity_users_deleted_total"],
                         [(("via", "event"),)] * 3, "each erased once, by the events pass")
        self.assertEqual(self.pass_errors, [])

    def test_two_in_a_row_in_the_events_pass_no_longer_fail_it_and_the_cursor_stays_before_their_events(self):
        """#121 failed this pass on the second read with no answer, before it reached the oldest event (#152 shape 1:
        two hanging people with pending DELETE_ACCOUNT events, and nobody between them). Now the pass goes on past
        them, erases past them, and the cursor stays before the oldest event not acted on."""
        by_kc = {u["kc"]: u for u in self.people("ada", "bob", "carol")}
        ada, bob, carol = by_kc["ada"], by_kc["bob"], by_kc["carol"]
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["erased"], 0, "the cursor is set")
        self.kc.gone.add("carol")
        self.kc.no_answer.update({"ada": httpx2.ReadTimeout, "bob": httpx2.ReadError})
        self.kc.events += [self.event("carol", 1_000), self.event("ada", 2_000), self.event("bob", 3_000)]
        ada_at, bob_at = self.kc.events[1]["time"], self.kc.events[2]["time"]

        self.assertEqual(self.tick(worker, T0 + 4)["erased"], 1, "newest first: bob's read, then ada's, got no answer; carol was reached")
        self.assertEqual(self.pass_errors, [], "two reads with no answer in a row failed the pass (#121)")
        self.assertEqual(self.kc.read, ["bob", "bob", "ada", "carol"], "bob's ReadError came back at once: once more, on another connection (#182)")
        self.assertIsNone(self.row(carol))
        self.assertEqual(self.cursor(), ada_at, "the cursor stays before the oldest event of a person whose erasure did not happen")
        self.assertIsNotNone(self.row(ada))
        self.assertIsNotNone(self.row(bob))
        self.assertEqual(self.errors_counted(), 2)

        self.kc.no_answer.clear()                                   # Keycloak answers about them: both are gone
        self.kc.gone.update({"ada", "bob"})
        self.assertEqual(self.tick(worker, T0 + 34)["erased"], 2, "no event was passed over")
        for user in (ada, bob):
            self.assertIsNone(self.row(user))
        self.assertEqual(self.cursor(), bob_at, "every event acted on: the cursor moves")
        self.assertEqual(self.pass_errors, [])


if __name__ == "__main__":
    unittest.main()
