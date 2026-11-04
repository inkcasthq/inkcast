# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #470: follow-ups of the checkpoint that cuts the write-ahead log after an erasure (#404).

1. The erasure's checkpoint blocks nothing else in the process. ``checkpoint_wal`` waits up to WAL_CHECKPOINT_WAIT_MS
for a reader on an older snapshot (a backup's copy, another process's long read). It ran that wait under the store's
connection lock, inside SQLite (which holds the write lock meanwhile), on the event loop: ``/account/return`` and the
Keycloak sync's passes erase from async code, so with a reader pinning the log every request in the process waited for
it, 281 ms per erasure in the review of #404. Now the erasure runs in a worker thread and the checkpoint waits between
attempts, holding neither lock.

Each test pins the log with a reader on another connection, lets the erasure's checkpoint start, and then sends
another person's requests: a page that reads the store and an API call that writes it. Both must be answered while the
checkpoint still waits; the reader then ends, and the checkpoint cuts the log. The checkpoint may wait WAIT here rather
than 250 ms, so that the code before the fix shows its blocked loop rather than racing the default wait.

2. Housekeeping's checkpoint, the one that cuts the log when the erasure's own met a reader, said nothing an alert
could count when a reader held it off too (an INFO line with no event), and a step before it that raised skipped it
for the run. Now each run it cannot cut the log is a catalogued warning, and it runs whatever the steps before it did.
The bytes are read from the files, as in tests/test_erasure_wal.py.

3. ``take_backup`` removes the staging copy a killed restore left (#404) before it copies anything; an error in that
removal (opening the directory, the stat, the unlink) stopped the backup. Now it is a warning, and the backup is taken."""

import asyncio
import logging
import pathlib
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import httpx2

from inkcast.launcher import build, resolve
from inkcast.libs.db import Store, database
from inkcast.libs.db.backup import take_backup, verify_backup
from inkcast.libs.http.app import compose
from inkcast.libs.observability import EVENTS
from inkcast.services.identity.keycloak_sync import KeycloakSync
from inkcast.services.scheduler import housekeeping
from test_erasure_wal import KEY, APersonWithContent, context, recorded
from test_sync_breaker import PREFIX, Scenario

DAY = 86400
WAIT = 2.0
PENDING = "identity.erasure.wal_pending"
DEFERRED = "scheduler.housekeeping.wal_pending"


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


class HousekeepingsCheckpoint(APersonWithContent):
    """An erasure whose own checkpoint met a reader on an older snapshot (#404), then housekeeping."""

    def cut(self, records: list[logging.LogRecord]) -> list[tuple[int, str | None, int]]:
        """What housekeeping said about the write-ahead log: level, event and frames, one tuple per line."""
        return [(r.levelno, getattr(r, "event", None), getattr(r, "frames", None)) for r in records
                if "write-ahead log" in r.getMessage()]

    def test_a_run_a_reader_still_holds_off_is_a_catalogued_warning(self):
        reader = self.pinned_by_a_reader()
        self.assertEqual(self.store.erase_user(self.user["id"])["users"], 1)
        logging.getLogger("inkcast.services.scheduler").setLevel(logging.INFO)
        self.addCleanup(logging.getLogger("inkcast.services.scheduler").setLevel, logging.NOTSET)
        with recorded("inkcast.services.scheduler") as records:
            housekeeping(context(self.store))
        self.assertEqual(self.holding(), ["inkcast.db-wal"], "the reader still holds its snapshot: the log keeps her")
        ((level, event, frames),) = self.cut(records) or [(None, None, None)]
        self.assertEqual((level, event), (logging.WARNING, DEFERRED), "a line an alert can count, each run")
        self.assertIn(DEFERRED, EVENTS)
        self.assertGreater(frames, 0)
        self.assertNotIn(self.marker, "".join(r.getMessage() for r in records))
        reader.execute("COMMIT")
        with recorded("inkcast.services.scheduler") as records:
            housekeeping(context(self.store))
        self.assertEqual(self.holding(), [], "the next run, the reader gone, cuts it")
        self.assertEqual(self.cut(records), [], "and says nothing")

    def raised_before_it(self, step: str) -> None:
        """The store's ``step`` raises in housekeeping, after the reader is gone: the checkpoint runs all the same."""
        reader = self.pinned_by_a_reader()
        self.assertEqual(self.store.erase_user(self.user["id"])["users"], 1)
        reader.execute("COMMIT")                                        # gone before housekeeping runs
        with mock.patch.object(self.store, step, side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):          # the loop logs it, as before
                housekeeping(context(self.store))
        self.assertEqual(self.holding(), [], f"{step} raised, and the checkpoint after it ran all the same")
        self.assertEqual(self.wal_size(), 0)

    def test_the_cursor_sync_raising_does_not_skip_it(self):
        self.raised_before_it("sync_consumers")

    def test_a_purge_raising_does_not_skip_it(self):
        self.raised_before_it("purge_expired")

    def test_the_orphan_sweep_raising_does_not_skip_it(self):
        self.raised_before_it("sweep_orphans")


class TheBackupsStagingCleanup(unittest.TestCase):
    def test_a_removal_that_fails_still_lets_the_backup_be_taken(self):
        folder = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        db = folder / "inkcast.db"
        store = Store(str(db), KEY)
        store.upsert_user("oidc:kc#on-call", "on-call", None, None)
        store.close()
        # A directory where the staging copy would be: its unlink fails (EISDIR on Linux, EPERM on macOS), as one the
        # file system refuses would.
        (folder / ".inkcast.db.restoring").mkdir()
        with self.assertLogs("inkcast.libs.db.backup", logging.WARNING) as logs:
            backup = take_backup(db, folder / "backups", version="test", now=4e9)
        self.assertEqual(verify_backup(backup.path)["rows"]["users"], 1, "the backup is taken, and it is good")
        (line,) = logs.records
        self.assertIn("could not be removed", line.getMessage())
        self.assertNotIn(str(folder), line.getMessage(), "the error's name, not its text: no path")
        self.assertTrue((folder / ".inkcast.db.restoring").is_dir(), "left as it is, for the next backup to try")


if __name__ == "__main__":
    unittest.main()
