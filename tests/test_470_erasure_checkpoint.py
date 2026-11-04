# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #470: the checkpoint after an erasure (#404) blocks nothing else in the process.

``checkpoint_wal`` waits up to WAL_CHECKPOINT_WAIT_MS for a reader on an older snapshot (a backup's copy, another
process's long read). It ran that wait under the store's connection lock, inside SQLite (which holds the write lock
meanwhile), on the event loop: ``/account/return`` and the Keycloak sync's passes erase from async code, so with a
reader pinning the log every request in the process waited for it, 281 ms per erasure in the review of #404. Now the
erasure runs in a worker thread and the checkpoint waits between attempts, holding neither lock.

Each test pins the log with a reader on another connection, lets the erasure's checkpoint start, and then sends
another person's requests: a page that reads the store and an API call that writes it. Both must be answered while the
checkpoint still waits; the reader then ends, and the checkpoint cuts the log. The checkpoint may wait WAIT here rather
than 250 ms, so that the code before the fix shows its blocked loop rather than racing the default wait."""

import asyncio
import sqlite3
import threading
import unittest
from unittest import mock

import httpx2

from inkcast.launcher import build, resolve
from inkcast.libs.db import database
from inkcast.libs.http.app import compose
from inkcast.services.identity.keycloak_sync import KeycloakSync
from test_erasure_wal import recorded
from test_sync_breaker import PREFIX, Scenario

DAY = 86400
WAIT = 2.0
PENDING = "identity.erasure.wal_pending"


class AnErasureWithAReaderPinningTheLog(Scenario):
    """The IdentityService over one database file, a mock Keycloak, Ada who deleted her account there, and Bob, signed
    in, whose requests arrive while her erasure's checkpoint waits."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(database, "WAL_CHECKPOINT_WAIT_MS", int(WAIT * 1000))
        patch.start()
        self.addCleanup(patch.stop)
        self.specs = resolve("IdentityService")
        self.ctx = build(self.config(30), self.specs, transport=httpx2.MockTransport(self.kc))
        self.addCleanup(self.ctx.store.close)
        self.addCleanup(lambda: asyncio.run(self.ctx.http.aclose()))
        self.ada, self.bob = self.person("kc-ada"), self.person("kc-bob")
        self.kc.gone.add(self.ada["kc"])
        self.started, self.finished, self.checkpoints = threading.Event(), threading.Event(), []
        real = self.ctx.store.checkpoint_wal

        def watched():
            self.started.set()
            try:
                self.checkpoints.append(real())
                return self.checkpoints[-1]
            finally:
                self.finished.set()
        self.ctx.store.checkpoint_wal = watched

    def person(self, name: str) -> dict:
        user = self.store.upsert_user(PREFIX + name, name, None, None)
        self.store.save_token(f"ic_at_{name:x<43}", "access", "c1", user["id"], ["inkcast"], None, f"f-{name}", 10 * DAY)
        self.store.create_post(group_id="g", user_id=user["id"], platform="fake", title="T", status="scheduled",
                               run_at=4e9, payload={"platform": "fake", "title": "T", "payload": {}, "preview": {}})
        return {**self.store.get_user(user["id"]), "kc": name}

    def pinned(self) -> sqlite3.Connection:
        """Another process's connection holding a snapshot from before the erasure."""
        reader = sqlite3.connect(self.database)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM users").fetchall()
        return reader

    def served_meanwhile(self, erasure) -> tuple:
        """``erasure`` (a coroutine function of the client) started; once its checkpoint has begun, Bob's two requests;
        then the reader ends. What the erasure returned, Bob's two answers, and whether both came while the checkpoint
        still waited."""

        async def run():
            reader = self.pinned()
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=compose(self.specs, self.ctx)),
                                          base_url="http://localhost:8787") as client:
                task = asyncio.ensure_future(erasure(client))
                while not self.started.is_set() and not task.done():
                    await asyncio.sleep(0.001)
                page = await client.get("/account/return", headers={"Cookie": "inkcast_deletion=no-such-deletion"})
                link = await client.post("/api/v1/account/deletion-link",
                                         headers={"Authorization": f"Bearer ic_at_{self.bob['kc']:x<43}"})
                meanwhile = not self.finished.is_set()
                reader.execute("COMMIT")
                return await task, page, link, meanwhile

        with recorded("inkcast.libs.db.erasure") as self.records:
            return asyncio.run(run())

    def assert_served_meanwhile(self, page, link, meanwhile) -> None:
        self.assertTrue(meanwhile, "both answered while the erasure's checkpoint still waited for the reader")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("<h1>Back at Inkcast</h1>", page.text, "a page that reads the store")
        self.assertEqual(link.status_code, 200, link.text)
        self.assertIn("/account/delete/", link.json()["url"], "an API call that writes it")
        self.assertEqual([busy for busy, _, _ in self.checkpoints], [0], "then it cut the log, once the reader ended")
        self.assertEqual([r for r in self.records if getattr(r, "event", "") == PENDING], [], "nothing left pending")
        self.assertIsNone(self.store.get_user(self.ada["id"]), "Ada is erased")
        self.assertIsNotNone(self.store.get_user(self.bob["id"]), "Bob is not")

    def test_the_visit_back_from_keycloak(self):
        req = self.store.open_deletion_link(self.store.create_deletion_request(self.ada["id"], 900))
        cookie = self.store.arm_deletion_return(req["id"], self.ada["kc"], 1800)

        async def visit(client):
            return await client.get("/account/return", headers={"Cookie": f"inkcast_deletion={cookie}"})

        back, page, link, meanwhile = self.served_meanwhile(visit)
        self.assertIn("Your account is deleted", back.text)
        self.assert_served_meanwhile(page, link, meanwhile)

    def test_the_keycloak_sync_s_pass(self):
        sync = KeycloakSync(self.ctx.config, self.ctx.store, self.ctx.http, self.ctx.metrics, clock=lambda: self.now)
        sync.last_reconcile = self.now                                   # the events pass alone
        self.assertEqual(asyncio.run(sync.tick())["erased"], 0, "the cursors are set")
        self.kc.events.append({"time": int(self.now * 1000) + 1000, "type": "DELETE_ACCOUNT", "userId": self.ada["kc"]})
        self.now += 2

        async def a_pass(client):
            return await sync.tick()

        done, page, link, meanwhile = self.served_meanwhile(a_pass)
        self.assertEqual(done["erased"], 1)
        self.assert_served_meanwhile(page, link, meanwhile)


if __name__ == "__main__":
    unittest.main()
