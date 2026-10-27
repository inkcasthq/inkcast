# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #182: a stale connection is retried once, on a new connection, before it counts against a person; a token
endpoint 5xx is Keycloak failing, not the service account refused.

#152 classed every ``ReadError`` and ``RemoteProtocolError`` on a read about one person as that person getting no
answer: counted against them and the pass's budget, logged with their id, and the reconcile reads them last from
then on. Both also happen when the connection, not the person, is at fault: a keep-alive connection the server or a
router closed while it was idle fails the next request at once (``RemoteProtocolError``, "server disconnected without
sending a response"), and a Keycloak pod restart resets the connection under a read (``ReadError``). Now such a read is
made once more, on another connection (httpcore2 closes a connection that raised mid-request and the pool drops it, so
the retry never reuses it), and only when the error came back at once (``STALE_WITHIN``): a connection cut after
waiting, by a proxy or firewall that gave up on a lookup that hangs, is the hang itself, counted at once as before, so
a retry never doubles what a hang costs the pass and nothing escapes the budget. A ``ReadTimeout`` is never retried.
Only GET reads about a person are retried: no write is made twice. A token-endpoint answer of 5xx fails the pass as
Keycloak failing, not as the service account refused, which the runbook reads as a secret mismatch. Keycloak ids
carry non-hex letters (``kc-ada``) so an Inkcast id (``usr_<hex>``) can't contain one by chance."""

import asyncio
import unittest

import httpx2
from test_sync_per_person_errors import SyncError, T0
from test_sync_person_timeouts import DETAIL, Keycloak as _Keycloak
from test_sync_timeout_budget import Budget

from inkcast.services.identity.keycloak_sync import STALE_WITHIN

STALE = (httpx2.ReadError, httpx2.RemoteProtocolError)


class Keycloak(_Keycloak):
    """The #152 fake, plus connections that fail a read once: ``stale`` (Keycloak id -> the errors its next reads
    raise, one each, then it answers), ``events_stale`` (likewise for the DELETE_ACCOUNT events read about them),
    ``token_stale`` (the token endpoint, once), ``token_status`` (the token endpoint answers that status) and
    ``after`` (Keycloak id -> how long the failing read waited before the error, taken from the scenario's clock: a
    proxy that cut a lookup that hangs)."""

    def __init__(self, advance):
        super().__init__()
        self.stale: dict[str, list[type[httpx2.TransportError]]] = {}
        self.events_stale: dict[str, list[type[httpx2.TransportError]]] = {}
        self.token_stale: list[type[httpx2.TransportError]] = []
        self.token_status: "int | None" = None
        self.after: dict[str, float] = {}
        self.tokens = 0
        self.advance = advance

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path, q = request.url.path, request.url.params
        if path.endswith("/token"):
            self.tokens += 1
            if self.token_stale:
                raise self.token_stale.pop(0)(DETAIL, request=request)
            if self.token_status:
                return httpx2.Response(self.token_status, json={"error": "unknown_error"})
            return super().__call__(request)
        if path.endswith("/events") and self.events_stale.get(q.get("user")):
            raise self.events_stale[q.get("user")].pop(0)(DETAIL, request=request)
        if path.endswith(("/admin-events", "/events")):
            return super().__call__(request)
        keycloak_id = path.rsplit("/", 1)[1]
        if self.stale.get(keycloak_id):
            self.read.append(keycloak_id)
            self.advance(self.after.get(keycloak_id, 0.0))
            raise self.stale[keycloak_id].pop(0)(DETAIL, request=request)
        return super().__call__(request)


class Stale(Budget):
    def setUp(self):
        super().setUp()
        self.kc = Keycloak(self.advance)
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.kc))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AStaleConnectionIsRetriedOnce(Stale):
    def test_a_read_that_fails_at_once_is_made_again_and_the_person_is_acted_on(self):
        """The first read of the pass goes out on a keep-alive connection the router closed: it fails at once. The
        read is made again, answers 404, and the person is cut off as if nothing happened: no person error, no
        budget, no line, not remembered as hanging."""
        for error in STALE:
            with self.subTest(error=error.__name__):
                self.setUp()
                gone, fine = self.people("kc-ada", "kc-bob")
                self.kc.stale[gone["kc"]] = [error]
                self.kc.gone.add(gone["kc"])
                worker = self.sync(30)
                with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
                    done = self.reconcile(worker, T0)
                self.assertEqual(self.pass_errors, [])
                self.assertEqual(self.kc.read, [gone["kc"], gone["kc"], fine["kc"]], "once more, then on")
                self.assertEqual((done["checked"], done["disabled"]), (2, 1))
                self.assert_cut_off(gone)
                self.assertEqual(self.row(gone)["deleted_at"], T0)
                self.assert_untouched(fine)
                self.assertEqual(self.errors_counted(), 0, "the connection's failure, not the person's")
                self.assertEqual(self.failed(logs), [])
                self.assertEqual(self.ended(logs), [])
                self.assertEqual(worker._hanging, {}, "not known to hang: they answered")
                self.assertEqual(worker.last_reconcile, T0)

    def test_three_such_people_cost_the_pass_no_budget(self):
        """Three reads that fail at once and answer when made again: nothing of the budget is used, so the pass reads
        everyone and ends as a pass that read everyone. On main the three were the budget, and the pass failed."""
        people = self.people(*(f"kc-p{i}" for i in range(4)))
        for user in people[:3]:
            self.kc.stale[user["kc"]] = [httpx2.RemoteProtocolError]
        self.kc.disabled.add(people[3]["kc"])
        worker = self.sync(30)
        done = self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [], "three stale connections were three timeouts: the budget, nothing answered")
        self.assertEqual(len(self.kc.read), 7, "three read twice, one once")
        self.assertEqual((done["checked"], done["disabled"]), (4, 1))
        self.assert_cut_off(people[3])
        self.assertEqual(self.errors_counted(), 0)
        self.assertEqual(worker.last_reconcile, T0, "a reconcile of everyone")

    def test_in_the_events_pass_too_and_for_the_events_read(self):
        """The DELETE_ACCOUNT pass's read of the person, and the reconcile's read of a gone person's events, go through
        the same path: each is made again once."""
        gone, late = self.people("kc-ada", "kc-bob")
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["erased"], 0, "the cursors are set")
        self.kc.gone.update({gone["kc"], late["kc"]})
        self.kc.events.append(self.event(gone["kc"], 1_000))
        self.kc.stale[gone["kc"]] = [httpx2.ReadError]
        self.kc.events_stale[late["kc"]] = [httpx2.RemoteProtocolError]  # the reconcile asks for their missed event
        done = self.reconcile(worker, T0 + 5)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(done["erased"], 1, "erased by the events pass, at the second read")
        self.assertIsNone(self.row(gone))
        self.assertEqual(self.cursor(), self.kc.events[-1]["time"], "the event was acted on: the cursor moves")
        self.assertEqual(self.kc.read[:2], [gone["kc"], gone["kc"]])
        self.assert_cut_off(late)
        self.assertEqual(self.row(late)["deleted_at"], T0 + 5, "an administrator's deletion: the grace period runs")
        self.assertEqual(self.errors_counted(), 0)


class OnceOnly(Stale):
    def test_a_read_that_fails_at_once_twice_counts_against_the_person_once(self):
        """Made again, and the new connection fails too: that is the person's, as before, once: one error, one
        of the budget, one line, and read last by the next reconcile."""
        hangs, gone = self.people("kc-ada", "kc-bob")
        self.kc.stale[hangs["kc"]] = [httpx2.ReadError, httpx2.RemoteProtocolError]
        self.kc.gone.add(gone["kc"])
        worker = self.sync(30)
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(self.kc.read, [hangs["kc"], hangs["kc"], gone["kc"]], "twice, not three times")
        self.assertEqual((done["checked"], done["disabled"]), (2, 1))
        self.assert_cut_off(gone)
        self.assert_untouched(hangs)
        self.assertEqual(self.errors_counted(), 1)
        (record,) = self.failed(logs)
        self.assertEqual((record.user_id, record.status, record.cause), (hangs["id"], 0, "RemoteProtocolError"))
        self.assertNotIn(DETAIL, record.getMessage())
        self.assertEqual(set(worker._hanging), {hangs["kc"]})

    def test_the_budget_counts_the_people_not_the_attempts(self):
        """Three people whose connections fail at once, twice each: three of the budget, six reads, and the pass
        fails as a whole since no read about a person answered (an outage looks like this too)."""
        people = self.people("kc-ada", "kc-bob", "kc-carol", "kc-dave")
        for user in people[:3]:
            self.kc.stale[user["kc"]] = [httpx2.ReadError, httpx2.ReadError]
        worker = self.sync(30)
        self.assertIsNone(self.reconcile(worker, T0))
        self.assertIs(type(self.pass_errors[0]), SyncError)
        self.assertEqual(len(self.kc.read), 6, "each of the three made again once; the fourth not tried")
        self.assertEqual(self.errors_counted(), 3)
        self.assertEqual(worker.unread_until, T0 + 600, "#119: nobody could be read")


class AHangIsNotRetried(Stale):
    def test_a_connection_cut_after_waiting_is_the_hang_itself_and_counts_at_once(self):
        """A proxy or firewall between Inkcast and Keycloak gives up on a lookup that hangs and closes the connection
        at its own timeout: the same errors, after waiting. A retry would wait as long again and the budget would
        bound twice the time it promises, so the read is not made again: counted as before, at once."""
        for error, waited in ((httpx2.ReadError, STALE_WITHIN), (httpx2.RemoteProtocolError, 30.0)):
            with self.subTest(error=error.__name__, waited=waited):
                self.setUp()
                people = self.people("kc-ada", "kc-bob", "kc-carol", "kc-dave", "kc-eve")
                gone, cut, untried = people[0], people[1:4], people[4]
                self.kc.gone.add(gone["kc"])                       # answers first: the pass is a success for what it did
                for user in cut:
                    self.kc.stale[user["kc"]] = [error]            # it would answer at a second read: never made
                    self.kc.after[user["kc"]] = waited
                worker = self.sync(30)
                with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
                    done = self.reconcile(worker, T0)
                self.assertEqual(self.pass_errors, [])
                self.assertEqual(self.kc.read, [u["kc"] for u in people[:4]], "once each: the budget, and the fifth untried")
                self.assertEqual((done["checked"], done["disabled"]), (4, 1))
                self.assert_cut_off(gone)
                self.assertEqual(self.errors_counted(), 3)
                self.assertEqual(len(self.failed(logs)), 3)
                (ended,) = self.ended(logs)
                self.assertEqual((ended.timeouts, ended.stage), (3, "reconcile"), "the budget ended the pass: three reads, not six")
                self.assert_untouched(untried)
                self.assertEqual(set(worker._hanging), {u["kc"] for u in cut})

    def test_a_read_timeout_is_never_retried(self):
        hangs, gone = self.people("kc-ada", "kc-bob")
        self.kc.stale[hangs["kc"]] = [httpx2.ReadTimeout]
        self.kc.gone.add(gone["kc"])
        worker = self.sync(30)
        done = self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(self.kc.read, [hangs["kc"], gone["kc"]], "the client waited its whole timeout: that is the hang")
        self.assertEqual((done["checked"], done["disabled"]), (2, 1))
        self.assertEqual(self.errors_counted(), 1)
        self.assert_untouched(hangs)


class NoWriteIsMadeTwice(Stale):
    def test_the_token_request_is_not_retried(self):
        """The retry is for GET reads about a person. The token endpoint's POST is not read again on a stale
        connection: the pass fails, as for any transport error there, and the next pass asks again."""
        for error in STALE:
            with self.subTest(error=error.__name__):
                self.setUp()
                first, after = self.people("kc-ada", "kc-bob")
                self.kc.token_stale = [error]
                worker = self.sync(30)
                self.assertIsNone(self.reconcile(worker, T0))
                self.assertIs(type(self.pass_errors[0]), SyncError)
                self.assertIsInstance(self.pass_errors[0].__cause__, error)
                self.assertEqual(self.kc.tokens, 1, "one request to the token endpoint")
                self.assertEqual(self.kc.read, [], "nobody was read")
                self.assertEqual(self.errors_counted(), 0)
                self.assert_untouched(after)

    def test_a_transport_error_on_the_way_is_still_the_passes_at_once(self):
        """Shape 6 of #152 stands: a connection refused is not a stale connection, and is not retried."""
        first, after = self.people("kc-ada", "kc-bob")
        self.kc.stale[first["kc"]] = [httpx2.ConnectError]
        worker = self.sync(30)
        self.assertIsNone(self.reconcile(worker, T0))
        self.assertIs(type(self.pass_errors[0]), SyncError)
        self.assertEqual(self.kc.read, [first["kc"]])
        self.assertEqual(self.errors_counted(), 0)


class ATokenEndpointFiveHundred(Stale):
    def test_is_logged_as_keycloak_failing_not_as_the_service_account_refused(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.setUp()
                (person,) = self.people("kc-ada")
                self.kc.token_status = status
                worker = self.sync(30)
                self.assertIsNone(self.reconcile(worker, T0))
                self.assertIs(type(self.pass_errors[0]), SyncError)
                message = str(self.pass_errors[0])
                self.assertNotIn("refused", message)
                self.assertIn(f"HTTP {status}", message)
                self.assertIn("token endpoint", message)
                self.assertEqual(self.kc.read, [])
                self.assert_untouched(person)

    def test_a_401_is_still_a_refusal(self):
        (person,) = self.people("kc-ada")
        self.kc.refuse_token = True
        worker = self.sync(30)
        self.assertIsNone(self.reconcile(worker, T0))
        self.assertIn("refused the sync service account (HTTP 401)", str(self.pass_errors[0]))


if __name__ == "__main__":
    unittest.main()
