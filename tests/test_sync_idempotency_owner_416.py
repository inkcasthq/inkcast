# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #416: a Git sync request frees or completes only its own Idempotency-Key reservation.

A reservation pending for longer than ``IDEMPOTENCY_PENDING_SECONDS`` (a sync that outlived it) is taken over by the
next repeat of the request. On main the first request, when it ended, released or completed the key by (user, key)
alone: it freed the repeat's reservation, or wrote its own result into it, and a third request ran, or was answered,
beside the repeat. Now the reservation carries its owner, a token made when it was reserved, and release and complete
match it. A release or complete that fails (the database busy) is logged by user id, never with the key, and the
sync's own outcome still reaches the client. The syncs are held on events and the clock is injected: no sleeps."""

import asyncio
import contextlib
import functools
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx2

from inkcast.libs.db import Store
from inkcast.libs.http.rest import ApiError
from test_e2e import Harness, connect_through_page
from test_git_publishing import story

KEY = "k" * 40
LOGGER = "inkcast.services.publishing.rest"


class ARequestFreesOnlyItsOwnReservation(Harness):
    """The stack with DEV connected and a CI token. ``held_syncs`` replaces the sync with one that signals when it runs
    and waits to be let go, call by call, so the first request can be held past the pending window while a twin takes
    its key over, and ended while the twin still runs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = cls.config.public_url
        h = {"Authorization": f"Bearer {cls('test_a_third_request_waits_on_the_twin_that_took_the_key_over').access_token()}"}

        async def setup():
            async with httpx2.AsyncClient(timeout=30) as c:
                link = (await c.post(f"{cls.base}/api/v1/platforms/devto/connect-link", headers=h)).json()
                await connect_through_page(c, link["url"], {"api_key": "devto-key"})
                r = await c.post(f"{cls.base}/api/v1/tokens", headers=h, json={"name": "blog CI", "scopes": ["stories:sync"]})
                return r.json()
        cls.pat = asyncio.run(setup())
        cls.user_id = cls.app.store._one("SELECT user_id FROM api_tokens WHERE id = ?", cls.pat["id"])["user_id"]
        cls.gitsync = cls.app.extras["publishing"][1]          # the GitSync the route closes over

    def body(self, path: str, title: str) -> dict:
        return {"source": f"github.com/acme/blog-{self.id()}", "files": [{"path": path, "markdown": story(title, platforms="[devto]")}]}

    def post(self, body: dict, key: str) -> httpx2.Response:
        async def run():
            async with httpx2.AsyncClient(timeout=30) as c:
                return await c.post(f"{self.base}/api/v1/stories:sync", json=body,
                                    headers={"Authorization": f"Bearer {self.pat['token']}", "Idempotency-Key": key})
        return asyncio.run(run())

    def start(self, body: dict, key: str) -> tuple[threading.Thread, dict]:
        """``post`` in a thread of its own; the response lands in the dict when the thread ends."""
        out: dict = {}
        thread = threading.Thread(target=lambda: out.update(response=self.post(body, key)), daemon=True)
        thread.start()
        return thread, out

    @contextlib.contextmanager
    def held_syncs(self, count: int, first_raises: ApiError | None = None):
        """The first ``count`` syncs each set ``running`` when they start and wait for ``go``; the first raises
        ``first_raises`` instead of running when given. Later calls run at once. Yields the gates and the calls made."""
        real = self.gitsync.sync
        gates = [(threading.Event(), threading.Event()) for _ in range(count)]
        calls: list[int] = []

        async def held(user_id, req):
            n = len(calls)
            calls.append(n)
            if n < count:
                running, go = gates[n]
                running.set()
                await asyncio.get_running_loop().run_in_executor(None, functools.partial(go.wait, 30))
                if n == 0 and first_raises is not None:
                    raise first_raises
            return await real(user_id, req)

        try:
            with mock.patch.object(self.gitsync, "sync", held):
                yield gates, calls
        finally:
            for _, go in gates:                 # a failed assertion leaves no sync held
                go.set()

    @contextlib.contextmanager
    def clock_past_the_pending_window(self):
        """The ledger's clock, as the twin sees it: the first request's reservation has just expired."""
        later = time.time() + self.app.store.IDEMPOTENCY_PENDING_SECONDS + 1
        with mock.patch("inkcast.libs.db.ledger.time", SimpleNamespace(time=lambda: later)):
            yield

    def first_then_twin(self, body: dict, key: str, gates) -> tuple[tuple[threading.Thread, dict], tuple[threading.Thread, dict]]:
        """The first request held in its sync, then the twin that takes its expired key over, held in its own."""
        first = self.start(body, key)
        self.assertTrue(gates[0][0].wait(10), "the first sync is running")
        with self.clock_past_the_pending_window():
            twin = self.start(body, key)
            self.assertTrue(gates[1][0].wait(10), "the twin took the key over and is running")
        return first, twin

    def test_a_third_request_waits_on_the_twin_that_took_the_key_over(self):
        """The issue's scenario: the first ends with a result (on main, its complete wrote that result into the twin's
        reservation, and the third request was answered with it, replayed, while the twin still ran)."""
        body, key = self.body("posts/slow.md", "Slow"), "k-slow"
        with self.held_syncs(2) as (gates, calls):
            (first, first_out), (twin, twin_out) = self.first_then_twin(body, key, gates)
            gates[0][1].set()                                               # the first ends: 'create'
            first.join(30)
            self.assertEqual(first_out["response"].status_code, 200, first_out["response"].text)
            third = self.post(body, key)                                    # while the twin runs
            self.assertEqual((third.status_code, third.json().get("error")), (409, "idempotency_key_in_use"), third.text)
            gates[1][1].set()
            twin.join(30)
        self.assertEqual(twin_out["response"].status_code, 200, twin_out["response"].text)
        self.assertEqual(calls, [0, 1], "a third sync ran")
        replay = self.post(body, key)                                       # the twin's result is the one kept
        self.assertEqual((replay.headers.get("idempotent-replayed"), replay.json()), ("true", twin_out["response"].json()))

    def test_a_first_request_that_fails_leaves_the_twin_its_reservation(self):
        """The path reachable on main before #290: the first ends in an error, and its release freed the twin's key."""
        body, key = self.body("posts/failing.md", "Failing"), "k-failing"
        with self.held_syncs(2, first_raises=ApiError(503, "database_busy", "The database is busy.")) as (gates, calls):
            (first, first_out), (twin, twin_out) = self.first_then_twin(body, key, gates)
            gates[0][1].set()
            first.join(30)
            self.assertEqual(first_out["response"].status_code, 503, first_out["response"].text)
            third = self.post(body, key)
            self.assertEqual((third.status_code, third.json().get("error")), (409, "idempotency_key_in_use"), third.text)
            gates[1][1].set()
            twin.join(30)
        self.assertEqual(twin_out["response"].status_code, 200, twin_out["response"].text)
        self.assertEqual(calls, [0, 1], "a third sync ran")

    def test_a_result_reaches_the_client_when_its_key_cannot_be_completed(self):
        body, key = self.body("posts/kept.md", "Kept"), "k-kept"
        before = len(self.fakes.devto_articles)
        with mock.patch.object(self.app.store, "complete_idempotency_key",
                               side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs(LOGGER, level="ERROR") as logged:
            r = self.post(body, key)
        self.assertEqual(r.status_code, 200, r.text)                       # on main: 500, although the article was made
        self.assertEqual((r.json()["results"][0]["action"], len(self.fakes.devto_articles)), ("create", before + 1))
        [record] = logged.records
        self.assertIn(self.user_id, record.getMessage())
        self.assertNotIn(key, record.getMessage())                          # ids and fixed strings only, never the key
        self.assertIsNotNone(record.exc_info)
        again = self.post(body, key)                                        # the reservation stays until it expires
        self.assertEqual((again.status_code, again.json().get("error")), (409, "idempotency_key_in_use"), again.text)

    def test_a_failed_release_is_logged_and_the_sync_s_own_error_is_the_answer(self):
        body, key = self.body("posts/busy.md", "Busy"), "k-busy"
        with mock.patch.object(self.gitsync, "sync", mock.AsyncMock(side_effect=ApiError(503, "database_busy", "Busy."))), \
                mock.patch.object(self.app.store, "release_idempotency_key",
                                  side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs(LOGGER, level="ERROR") as logged:
            r = self.post(body, key)
        self.assertEqual((r.status_code, r.json()["error"]), (503, "database_busy"))   # the sync's error, not the release's
        [record] = logged.records
        self.assertIn(self.user_id, record.getMessage())
        self.assertNotIn(key, record.getMessage())
        self.assertIsNotNone(record.exc_info)


class TheReservationNamesItsOwner(unittest.TestCase):
    """The ledger alone: the token ``reserve_idempotency_key`` hands out is what ``release_`` and ``complete_`` match."""

    def setUp(self):
        self.store = Store(":memory:", KEY)
        self.addCleanup(self.store.close)
        self.uid = self.store.upsert_user("oidc:https://kc/realms/i#writer", "writer", None, None)["id"]

    def test_release_and_complete_match_the_owner(self):
        owner = self.store.reserve_idempotency_key(self.uid, "k", "h")
        self.assertIsInstance(owner, float)
        self.assertFalse(self.store.release_idempotency_key(self.uid, "k", owner + 1))
        self.assertEqual(self.store.reserve_idempotency_key(self.uid, "k", "h"), ("h", ""), "still pending, still held")
        self.assertFalse(self.store.complete_idempotency_key(self.uid, "k", owner + 1, '{"theirs": 1}'))
        self.assertEqual(self.store.reserve_idempotency_key(self.uid, "k", "h"), ("h", ""))
        self.assertTrue(self.store.complete_idempotency_key(self.uid, "k", owner, '{"mine": 1}'))
        self.assertEqual(self.store.reserve_idempotency_key(self.uid, "k", "h"), ("h", '{"mine": 1}'))
        self.assertFalse(self.store.release_idempotency_key(self.uid, "k", owner), "a completed key is not released")
        other = self.store.reserve_idempotency_key(self.uid, "k2", "h")
        self.assertTrue(self.store.release_idempotency_key(self.uid, "k2", other))
        self.assertNotEqual(self.store.reserve_idempotency_key(self.uid, "k2", "h"), ("h", ""), "free again")

    def test_a_reservation_taken_over_belongs_to_the_taker(self):
        now = time.time()
        with mock.patch("inkcast.libs.db.ledger.time", SimpleNamespace(time=lambda: now)):
            first = self.store.reserve_idempotency_key(self.uid, "k", "h")
        with mock.patch("inkcast.libs.db.ledger.time",
                        SimpleNamespace(time=lambda: now + self.store.IDEMPOTENCY_PENDING_SECONDS + 1)):
            twin = self.store.reserve_idempotency_key(self.uid, "k", "h")
            self.assertIsInstance(twin, float)
            self.assertNotEqual(twin, first)
            self.assertFalse(self.store.release_idempotency_key(self.uid, "k", first))
            self.assertFalse(self.store.complete_idempotency_key(self.uid, "k", first, '{"first": 1}'))
            self.assertEqual(self.store.reserve_idempotency_key(self.uid, "k", "h"), ("h", ""), "the twin's, pending")
            self.assertTrue(self.store.complete_idempotency_key(self.uid, "k", twin, '{"twin": 1}'))
            self.assertEqual(self.store.reserve_idempotency_key(self.uid, "k", "h"), ("h", '{"twin": 1}'))

    def test_a_person_without_a_row_holds_nothing_and_runs(self):
        """Issue #196's case, kept: the write is refused by the owner guard, and the request runs as if it held the key."""
        self.store.erase_user(self.uid)
        owner = self.store.reserve_idempotency_key(self.uid, "k", "h")
        self.assertIsInstance(owner, float)
        self.assertFalse(self.store.complete_idempotency_key(self.uid, "k", owner, "{}"))
        self.assertFalse(self.store.release_idempotency_key(self.uid, "k", owner))


class ARowThePreviousReleaseWrote(unittest.TestCase):
    def test_an_old_pending_row_is_nobody_s(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = directory + "/inkcast.db"
        old = Store(path, KEY)
        uid = old.upsert_user("oidc:https://kc/realms/i#writer", "writer", None, None)["id"]
        old.close()
        created = time.time()
        with sqlite3.connect(path) as c:
            c.execute("INSERT INTO idempotency VALUES (?, 'k-old', 'h', '', ?)", (uid, created))   # pending, from before
        store = Store(path, KEY)
        self.addCleanup(store.close)
        self.assertEqual(store.reserve_idempotency_key(uid, "k-old", "h"), ("h", ""), "held until it expires")
        self.assertFalse(store.release_idempotency_key(uid, "k-old", created + 1))   # no request of this release holds it
        with mock.patch("inkcast.libs.db.ledger.time",
                        SimpleNamespace(time=lambda: time.time() + store.IDEMPOTENCY_PENDING_SECONDS + 1)):
            self.assertIsInstance(store.reserve_idempotency_key(uid, "k-old", "h"), float, "then taken over")


if __name__ == "__main__":
    unittest.main()
