# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #152: a per-pass budget of reads with no answer, so two hanging people can't block every pass.

#121 (PR #146) made a read about one person that gets no answer that person's alone, and failed the pass on the
second in a row. Two people whose reads hang and who are read next to each other therefore failed every pass: two
with a pending DELETE_ACCOUNT event, once the people between them were erased; two edited within the same minute,
which the admin-events look-back re-reads at every pass. Now a pass skips each such person, does not read them again
in that pass, and ends early, as a success for what it did, once ``TIMEOUT_BUDGET`` (3) reads about people got no
answer; it fails as a whole only when no read about a person answered, so an outage still costs it the budget rather
than one timeout per person, and a reconcile that could read nobody sets ``unread_until`` (#119) as for a realm-wide
403. A transport error on the way to Keycloak (a connection refused, the client's own pool) is never one person's and
fails the pass at once. A read outside a pass (``/account/return``) counts for no pass. The reconcile reads the people
whose last read got no answer last, so they delay each other and nobody else. What must still hold: the DELETE_ACCOUNT
cursor never passes the event of a person whose erasure did not happen; a person whose read got no answer is neither
gone nor present (the breaker neither counts nor holds them); the erasure and cut-off re-checks (#102).

Each test fails on main. Keycloak ids carry non-hex letters (``kc-ada``) so an Inkcast id (``usr_<hex>``) can't
contain one by chance."""

import asyncio
import os
import tempfile
import unittest

import httpx2
from starlette.testclient import TestClient
from test_sync_per_person_errors import DAY, T0, USER_EVENTS_CURSOR, SyncError
from test_sync_person_timeouts import DETAIL, Keycloak, Scenario

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.http.app import compose
from inkcast.libs.secrets import SecretValue
from inkcast.services.identity.keycloak_sync import KeycloakSync

ISSUER = "https://kc.example/realms/inkcast"
PREFIX = f"oidc:{ISSUER}#"
KEY = "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE"
PERSONS = (httpx2.ReadTimeout, httpx2.ReadError, httpx2.RemoteProtocolError)   # the request reached Keycloak
ON_THE_WAY = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)  # it did not: never one person's


class Budget(Scenario):
    def admin_event(self, keycloak_id: str, offset_ms: int) -> None:
        self.kc.admin_events.append({"time": int(self.now * 1000) + offset_ms, "resourceType": "USER",
                                     "operationType": "UPDATE", "resourcePath": f"users/{keycloak_id}"})

    def ended(self, logs) -> list:
        return [r for r in logs.records if getattr(r, "event", "") == "identity.sync.pass.ended"]

    def passes(self, worker: KeycloakSync, start: float, n: int, every: float = 30.0) -> list:
        """``n`` passes, ``every`` seconds apart, the first at ``start``; the results (None for a failed pass)."""
        return [self.tick(worker, start + i * every) for i in range(n)]


class TwoHangingPeopleWithPendingDeleteAccountEvents(Budget):
    """Shape 1: both deleted their accounts, their reads hang, nobody is between them any more. The events pass used
    to fail on the second (no answer to two reads in a row), before the admin-events pass and the reconcile."""

    def test_the_others_are_cut_off_within_one_reconcile_interval_whichever_way_they_sort(self):
        for hanging_first in (True, False):
            with self.subTest(hanging_first=hanging_first):
                self.setUp()
                people = self.people("kc-ada", "kc-bob", "kc-carol", "kc-dave")
                first, second, gone, disabled = people if hanging_first else (*people[2:], *people[:2])
                worker = self.sync(30, reconcile_seconds=600)
                self.assertEqual(self.tick(worker)["erased"], 0, "the cursors are set")
                self.kc.no_answer.update({first["kc"]: httpx2.ReadTimeout, second["kc"]: httpx2.ReadTimeout})
                self.kc.gone.add(gone["kc"])                        # an administrator deleted them: for the reconcile
                self.kc.disabled.add(disabled["kc"])
                self.kc.events += [self.event(first["kc"], 1_000), self.event(second["kc"], 2_000)]
                first_at = self.kc.events[-2]["time"]
                results = self.passes(worker, T0 + 30, 20)          # one reconcile interval: the last pass reconciles
                self.assertEqual(self.pass_errors, [], "two hanging people next to each other failed the pass")
                self.assertTrue(all(isinstance(r, dict) for r in results))
                self.assertEqual(worker.last_reconcile, T0 + 600, "the reconcile ran, on schedule")
                self.assert_cut_off(gone)
                self.assertEqual(self.row(gone)["deleted_at"], T0 + 600)
                self.assert_cut_off(disabled)
                self.assert_untouched(first)
                self.assert_untouched(second)
                self.assertEqual(self.cursor(), first_at, "the cursor stays before the oldest event not acted on")
                self.assertEqual(self.kc.read.count(first["kc"]), 20, "read once per pass: their event holds the cursor")
                self.assertEqual(self.kc.read.count(second["kc"]), 20, "and not again by the reconcile in the same pass")
                self.assertEqual(self.errors_counted(), 40, "one person error per hanging person per pass")
                self.assertEqual(results[-1]["checked"], 2, "the reconcile read the two who answered")
                # Keycloak answers about them at last: both gone, erased by the events pass that holds their events.
                self.kc.no_answer.clear()
                self.kc.gone.update({first["kc"], second["kc"]})
                self.assertEqual(self.tick(worker, T0 + 660)["erased"], 2)
                self.assertIsNone(self.row(first))
                self.assertIsNone(self.row(second))
                self.assertEqual(self.cursor(), self.kc.events[-1]["time"], "every event acted on: the cursor moves")
                self.assertEqual(self.pass_errors, [])


class TwoHangingPeopleEditedWithinAMinute(Budget):
    """Shape 2: an administrator saved both within the same minute; in a quiet realm the admin-events look-back reads
    them both at every pass. The pass used to fail on the second, and the reconcile never ran."""

    def test_the_reconcile_runs_and_cuts_the_others_off_within_one_reconcile_interval(self):
        for hanging_first in (True, False):
            with self.subTest(hanging_first=hanging_first):
                self.setUp()
                people = self.people("kc-ada", "kc-bob", "kc-carol", "kc-dave")
                first, second, gone, disabled = people if hanging_first else (*people[2:], *people[:2])
                worker = self.sync(30, reconcile_seconds=600)
                self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
                self.kc.no_answer.update({first["kc"]: httpx2.ReadTimeout, second["kc"]: httpx2.ReadTimeout})
                self.kc.gone.add(gone["kc"])
                self.kc.disabled.add(disabled["kc"])
                self.admin_event(first["kc"], 1_000)
                self.admin_event(second["kc"], 2_000)
                results = self.passes(worker, T0 + 30, 20)
                self.assertEqual(self.pass_errors, [], "two hanging people in the look-back failed the pass")
                self.assertEqual([r["checked"] for r in results[:-1]], [2] * 19, "both re-read at every pass")
                self.assertEqual(results[-1]["checked"], 4, "the reconcile: the two who answered, the hanging two not again")
                self.assertEqual(worker.last_reconcile, T0 + 600)
                self.assert_cut_off(gone)
                self.assert_cut_off(disabled)
                self.assert_untouched(first)
                self.assert_untouched(second)
                self.assertEqual(self.errors_counted(), 40)
                self.assertEqual(self.kc.read.count(first["kc"]), 20)


class AWholeRealmOutage(Budget):
    """Every read about a person hangs (the user-storage provider is down for everyone; Keycloak's own lists still
    answer). The pass still fails, as a whole, and after the budget: three timeouts, not one per person."""

    def test_a_reconcile_in_which_nobody_answers_fails_after_the_budget_and_sets_unread_until(self):
        people = self.people(*(f"kc-p{i:02d}" for i in range(20)))
        for user in people:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        worker = self.sync(30, reconcile_seconds=600)
        self.assertIsNone(self.reconcile(worker, T0))
        self.assertIs(type(self.pass_errors[0]), SyncError, "the sync failed, not a person")
        self.assertEqual(len(self.kc.read), 3, f"{len(self.kc.read)} reads: the budget, not one timeout per person")
        self.assertEqual(self.errors_counted(), 3, "each read that got no answer is that person's failure too")
        self.assertEqual(worker.last_reconcile, 0, "not a reconcile that ran")
        self.assertEqual(worker.last_success, 0.0)
        self.assertEqual(worker.unread_until, T0 + 600, "#119: nobody could be read, so no full re-read before the next is due")
        for seconds in (30, 60, 570):                               # still failing, without reading anyone again
            self.assertIsNone(self.tick(worker, T0 + seconds))
        self.assertEqual(len(self.kc.read), 3, "no read until the next reconcile is due")
        self.assertIsNone(self.tick(worker, T0 + 600), "due again: read, still nobody answers")
        self.assertEqual(len(self.kc.read), 6)
        self.kc.no_answer.clear()                                   # the provider is back
        self.assertIsInstance(self.tick(worker, T0 + 1200), dict)
        self.assertEqual(len(self.kc.read), 26, "everyone read")
        self.assertEqual(worker.last_reconcile, T0 + 1200)
        for user in people:
            self.assert_untouched(user)

    def test_an_events_pass_in_which_nobody_answers_fails_after_the_budget_and_holds_the_cursor(self):
        people = self.people(*(f"kc-p{i}" for i in range(5)))
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["erased"], 0, "the cursor is set")
        was = self.cursor()
        for i, user in enumerate(people):                           # all five deleted their accounts
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
            self.kc.gone.add(user["kc"])
            self.kc.events.append(self.event(user["kc"], 1_000 * (i + 1)))
        self.assertIsNone(self.tick(worker, T0 + 6))
        self.assertIs(type(self.pass_errors[0]), SyncError)
        self.assertEqual(len(self.kc.read), 3, "the budget: the other two were not tried")
        self.assertEqual(self.cursor(), was, "the pass failed: the cursor did not move")
        self.assertEqual(self.errors_counted(), 3)
        for user in people:
            self.assertIsNotNone(self.row(user))
        self.kc.no_answer.clear()
        self.assertEqual(self.tick(worker, T0 + 36)["erased"], 5, "no event was passed over")
        self.assertEqual(self.cursor(), self.kc.events[-1]["time"])


class AnOutageThatBeginsMidPass(Budget):
    def test_the_pass_ends_early_as_a_success_for_what_was_done(self):
        """Two people answer (one gone), then Keycloak's provider stops answering: the reconcile reads three more,
        each hangs, and the pass ends there, acting on what it read. The sixth is not tried (that would cost
        another timeout), and the next reconcile is on schedule, not at the next tick."""
        people = self.people(*(f"kc-p{i}" for i in range(6)))
        gone, fine, *hang = people
        self.kc.gone.add(gone["kc"])
        for user in hang:
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        worker = self.sync(30, reconcile_seconds=600)
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [], "a success for what was done")
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (5, 1, 0))
        self.assertEqual(self.kc.read, [u["kc"] for u in people[:5]], "the sixth was not tried")
        self.assert_cut_off(gone)
        self.assertEqual(self.row(gone)["deleted_at"], T0)
        for user in hang + [fine]:
            self.assert_untouched(user)
        self.assertEqual(self.errors_counted(), 3)
        self.assertEqual(self.pass_failures_counted(), 0)
        self.assertEqual((worker.last_reconcile, worker.last_success), (T0, T0), "a reconcile that ran")
        ended = self.ended(logs)
        self.assertEqual(len(ended), 1, "one line says the pass ended early")
        self.assertEqual((ended[0].timeouts, ended[0].stage), (3, "reconcile"))
        self.assertFalse(any(u["kc"] in ended[0].getMessage() for u in people), "no Keycloak id in the log")
        self.assertNotIn(DETAIL, ended[0].getMessage())
        reads = len(self.kc.read)
        self.tick(worker, T0 + 30)
        self.assertEqual(len(self.kc.read), reads, "the events path alone: the reconcile is not due")
        self.assertEqual(self.pass_errors, [])


class WhoseLastReadHungAreReadLast(Budget):
    def test_the_reconcile_reads_them_last_so_they_delay_each_other_and_nobody_else(self):
        """Four people whose reads hang sort first by Inkcast id, before the two who answer. The first reconcile
        does not know them yet and spends its budget on three of them: it could act on nobody. From the next on,
        the people who answer are read first, then the hanging ones, the longest ago first, so each gets its turn
        within the budget, and the ones who answer are always acted on."""
        people = self.people(*(f"kc-p{i}" for i in range(6)))
        h1, h2, h3, h4, gone, fine = people
        for user in (h1, h2, h3, h4):
            self.kc.no_answer[user["kc"]] = httpx2.ReadTimeout
        self.kc.gone.add(gone["kc"])
        worker = self.sync(30, reconcile_seconds=600)
        self.assertIsNone(self.reconcile(worker, T0), "not known to hang yet: the budget goes before anyone answers")
        self.assertEqual(self.kc.read, [h1["kc"], h2["kc"], h3["kc"]])
        self.assertEqual(worker.unread_until, T0 + 600)
        self.assert_untouched(gone)
        done = self.tick(worker, T0 + 600)
        self.assertIsInstance(done, dict)
        self.assertEqual(self.kc.read[3:], [h4["kc"], gone["kc"], fine["kc"], h1["kc"], h2["kc"]],
                         "whoever is not known to hang, in id order; then the hanging ones, the longest ago first")
        self.assertEqual((done["checked"], done["disabled"]), (5, 1))
        self.assert_cut_off(gone)
        self.assertEqual(worker.last_reconcile, T0 + 600)
        done = self.tick(worker, T0 + 1200)
        self.assertEqual(self.kc.read[8:], [gone["kc"], fine["kc"], h3["kc"], h4["kc"], h1["kc"]], "h3's turn: untried the longest")
        self.assertEqual(len(self.pass_errors), 1, "only the first reconcile failed")
        self.kc.no_answer.clear()                                   # all four fixed at Keycloak
        self.assertEqual(self.tick(worker, T0 + 1800)["checked"], 6)
        self.assertEqual(self.tick(worker, T0 + 2400)["checked"], 6, "answered: back in Inkcast id order")
        self.assertEqual(self.kc.read[-6:], [u["kc"] for u in people])


class NotTwiceInAPass(Budget):
    def test_a_person_whose_read_got_no_answer_is_not_read_again_by_the_same_pass(self):
        """One pass reads a person in the admin-events pass (an administrator saved them) and again in the reconcile:
        the second read would cost the pass another timeout for the same answer. They are read again at the next
        pass, as before."""
        hangs, gone = self.people("kc-ada", "kc-bob")
        worker = self.sync(30, reconcile_seconds=600)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.kc.no_answer[hangs["kc"]] = httpx2.ReadTimeout
        self.kc.gone.add(gone["kc"])
        self.admin_event(hangs["kc"], 1_000)
        done = self.reconcile(worker, T0 + 30)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(self.kc.read, [hangs["kc"], gone["kc"]], "once, by the admin-events pass; not by the reconcile")
        self.assertEqual((done["checked"], done["disabled"]), (2, 1))
        self.assertEqual(self.errors_counted(), 1, "one person error per person per pass")
        self.assert_cut_off(gone)
        self.assert_untouched(hangs)
        self.assertEqual(self.tick(worker, T0 + 60)["checked"], 1, "the next pass reads them again (the look-back)")
        self.assertEqual(self.kc.read, [hangs["kc"], gone["kc"], hangs["kc"]])


class TransportErrorsOnTheWayToKeycloak(Budget):
    """Shape 6: a connection refused or reset, a connect timeout, or the client's own pool exhausted happen before the
    request reaches Keycloak, so they are never about the person it was to ask about: the pass fails at once, as it
    does when the token endpoint gives no answer, and nobody is logged as failing."""

    def test_they_fail_the_pass_as_a_whole_at_once_and_are_nobodys(self):
        for error in ON_THE_WAY:
            with self.subTest(error=error.__name__, via="reconcile"):
                self.setUp()
                first, after = self.people("kc-ada", "kc-bob")
                self.kc.no_answer[first["kc"]] = error
                self.kc.gone.add(after["kc"])
                worker = self.sync(30)
                self.assertIsNone(self.reconcile(worker, T0))
                self.assertIs(type(self.pass_errors[0]), SyncError, "the sync failed, not a person (PersonError)")
                self.assertIsInstance(self.pass_errors[0].__cause__, error)
                self.assertNotIn(DETAIL, str(self.pass_errors[0]), "the error's name, nothing of its message")
                self.assertEqual(self.kc.read, [first["kc"]], "nobody after was tried")
                self.assertEqual(self.errors_counted(), 0, "not a person's failure")
                self.assertEqual(worker.last_reconcile, 0)
                self.assert_untouched(after)
            with self.subTest(error=error.__name__, via="event"):
                self.setUp()
                first, after = self.people("kc-ada", "kc-bob")
                worker = self.sync(30)
                self.assertEqual(self.tick(worker)["erased"], 0, "the cursor is set")
                was = self.cursor()
                self.kc.no_answer[first["kc"]] = error
                self.kc.gone.add(after["kc"])
                self.kc.events += [self.event(after["kc"], 1_000), self.event(first["kc"], 2_000)]   # first's is newer
                self.assertIsNone(self.tick(worker, T0 + 3))
                self.assertIs(type(self.pass_errors[0]), SyncError)
                self.assertEqual(self.kc.read, [first["kc"]])
                self.assertEqual(self.cursor(), was, "the pass failed: the cursor did not move")
                self.assertEqual(self.errors_counted(), 0)
                self.assertIsNotNone(self.row(after))

    def test_no_answer_once_the_request_reached_keycloak_is_the_persons(self):
        for error in PERSONS:
            with self.subTest(error=error.__name__):
                self.setUp()
                hangs, gone = self.people("kc-ada", "kc-bob")
                self.kc.no_answer[hangs["kc"]] = error
                self.kc.gone.add(gone["kc"])
                worker = self.sync(30)
                with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
                    done = self.reconcile(worker, T0)
                self.assertEqual(self.pass_errors, [])
                self.assertEqual((done["checked"], done["disabled"]), (2, 1))
                self.assert_cut_off(gone)
                self.assert_untouched(hangs)
                record = self.failed(logs)[0]
                self.assertEqual((record.user_id, record.status, record.cause), (hangs["id"], 0, error.__name__))
                self.assertNotIn(DETAIL, record.getMessage())


class AReadOutsideAPass(unittest.TestCase):
    """Shape 4: ``/account/return`` reads Keycloak through the worker's own ``KeycloakSync``, in the IdentityService's
    process, while a pass may be running. Its read answers or not on its own: it counts for no pass's budget, and a
    pass's budget does not decide what the page gets."""

    def setUp(self):
        self.kc = Keycloak()
        self.database = os.path.join(tempfile.mkdtemp(), "inkcast.db")
        self.store = Store(self.database, KEY)
        self.addCleanup(self.store.close)
        self.now = T0
        self.counted: list[tuple[str, tuple[tuple[str, str], ...]]] = []

    def person(self, keycloak_id: str) -> dict:
        user = self.store.upsert_user(PREFIX + keycloak_id, keycloak_id, None, None)
        self.store.save_token(f"ic_at_{keycloak_id:x<43}", "access", "c1", user["id"], ["inkcast"], None, f"f-{keycloak_id}", 10 * DAY)
        return {**user, "kc": keycloak_id}

    def armed(self, user: dict) -> str:
        """The person pressed Continue on the deletion page: the cookie that matches the armed request."""
        req = self.store.open_deletion_link(self.store.create_deletion_request(user["id"], 900))
        return self.store.arm_deletion_return(req["id"], user["kc"], 1800)

    def test_the_return_pages_read_does_not_move_the_workers_budget(self):
        hangs, gone = self.person("kc-ada"), self.person("kc-bob")
        cookie = self.armed(hangs)
        self.kc.no_answer[hangs["kc"]] = httpx2.ReadTimeout         # her own read hangs (the page's too)
        self.kc.gone.add(gone["kc"])
        config = Config(public_url="http://localhost:8787", secret_key=KEY, database_path=self.database, login="oidc",
                        oidc_issuer=ISSUER, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                        oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync",
                        keycloak_sync_secret=SecretValue("sync-secret"), keycloak_erase_after_days=30)
        config.validate()
        pages: list[httpx2.Response] = []
        client: "TestClient | None" = None

        def keycloak(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith(f"/users/{hangs['kc']}") and not pages:   # the worker's read of her: the
                pages.append(None)                                                 # page comes back meanwhile
                pages[0] = client.get("/account/return", cookies={"inkcast_deletion": cookie})
            return self.kc(request)

        specs = resolve("IdentityService")
        ctx = build(config, specs, transport=httpx2.MockTransport(keycloak))
        self.addCleanup(ctx.store.close)
        self.addCleanup(lambda: asyncio.run(ctx.http.aclose()))
        worker = KeycloakSync(config, ctx.store, ctx.http, ctx.metrics, clock=lambda: self.now)
        ctx.extras["keycloak_sync"] = worker                        # as run_keycloak_sync shares it with the routes
        client = TestClient(compose(specs, ctx), base_url="http://localhost:8787")
        self.addCleanup(client.close)
        with client:
            try:
                done = asyncio.run(worker.tick())                   # at start: the reconcile is due
            except Exception as exc:                                # a pass that failed as a whole
                self.fail(f"the page's read failed the worker's pass: {type(exc).__name__}: {exc}")
        self.assertEqual(len(pages), 1, "the page was served during the worker's pass")
        self.assertEqual(pages[0].status_code, 200)
        self.assertIn("being finished", pages[0].text, "no answer about her: nothing erased, the record kept")
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (2, 1, 0), "the worker's pass went on")
        self.assertIsNotNone(ctx.store.get_user(gone["id"])["disabled_at"], "bob, after her, is cut off")
        self.assertIsNotNone(ctx.store.get_user(hangs["id"]))
        self.assertIsNotNone(self.store.deletion_request_by_cookie(cookie), "the record stays for a later visit")
        self.assertEqual(self.kc.read, [hangs["kc"], hangs["kc"], gone["kc"]], "the page's read, the worker's, then bob")
        self.assertIn('inkcast_identity_sync_errors_total{stage="person"} 1', ctx.metrics.render(),
                      "the worker's failure counted; the page's read is nobody's failure")
        self.assertEqual(worker.last_reconcile, T0, "a reconcile that ran")


if __name__ == "__main__":
    unittest.main()
