# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #189 (issue #174): the per-post guard against a plug-in's odd exception, and where it sits.

Each test failed on the PR's head before the fix committed with it."""

import asyncio
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import httpx2

from inkcast.libs.content.story import parse_story
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.services.publishing.sync import GitSync


class Findable(Platform):
    """A platform that can be checked (``find``, like DEV): it remembers what it published and finds it again."""

    id, name, default_api_url, credential_fields = "findable", "Findable", "https://findable.invalid", []
    capabilities = frozenset({"find"})

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sent: list[str] = []

    async def verify(self, creds):
        return Account(id="1", name="findable")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sent.append(prepared.title)
        n = len(self.sent)
        return PublishResult(url=f"https://findable/{n}", remote_id=str(n), status="published")

    async def find_existing(self, creds, account, prepared):
        if prepared.title in self.sent:
            n = self.sent.index(prepared.title) + 1
            return PublishResult(url=f"https://findable/{n}", remote_id=str(n), status="published")
        return None


class BareError(PlatformError):
    """A plug-in's error that sets none of the flags the pipeline reads (it skips the base constructor)."""

    def __init__(self, message: str):
        Exception.__init__(self, message)


class Raising(Platform):
    """Raises the error it is given on every publish, after the request reached the platform, and counts the sends."""

    id, name, default_api_url, credential_fields = "raising", "Raising", "https://raising.invalid", []

    def __init__(self, error: BaseException):
        super().__init__(httpx2.AsyncClient())
        self.error, self.sends = error, 0

    async def verify(self, creds):
        return Account(id="1", name="raising")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        raise self.error


class OddErrorOnANowPost(unittest.TestCase):
    """The guard is only in ``run_due``. A "now" post goes through ``publish()`` and the same ``_send``, so the same
    odd error still escapes there (not new in the PR, but the case it is about)."""

    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.odd, self.fine = Raising(BareError("something went wrong")), Findable()
        for platform in (self.odd, self.fine):
            self.store.save_credentials(self.user, platform.id, {"k": "v"}, {"id": "1", "name": platform.id})
        self.platforms = {self.odd.id: self.odd, self.fine.id: self.fine}
        self.publisher = Publisher(self.store, self.platforms)

    def test_git_sync_does_not_send_a_now_post_again_after_an_odd_plugin_error(self):
        """Git sync's create path catches the escaped error, marks the ledger entry 'failed' and re-raises; the next
        sync then creates the post again: a second send of a post the platform may already have."""
        sync = GitSync(self.store, self.platforms, self.publisher)
        req = SyncRequest(source="github.com/acme/blog", files=[
            SyncFile(path="posts/p.md", markdown="---\ntitle: Now\nplatforms: [raising]\n---\n\nBody.\n")])
        try:
            with self.assertLogs("inkcast.libs.pipeline", "ERROR"):
                asyncio.run(sync.sync(self.user, req))
        except AttributeError:
            pass  # the PR's head: the odd error escapes the sync
        try:
            asyncio.run(sync.sync(self.user, req))  # the next commit's sync
        except AttributeError:
            pass  # the PR's head: it sent the post again, and the error escaped again
        self.assertEqual(self.odd.sends, 1, "a post that may already be out was sent again")
        self.assertEqual(self.store.get_entry(self.user, req.source, "posts/p.md", self.odd.id)["status"], "unconfirmed")

    def test_an_odd_error_on_one_platform_does_not_stop_the_others_in_a_now_publish(self):
        story = parse_story("# Now\n\nBody.\n", known_platforms=list(self.platforms))
        with self.assertLogs("inkcast.libs.pipeline", "ERROR"):
            outcomes = asyncio.run(self.publisher.publish(self.user, story, [self.odd.id, self.fine.id]))
        self.assertEqual([(o.platform, o.status) for o in outcomes], [("raising", "unconfirmed"), ("findable", "published")])
        self.assertEqual((self.odd.sends, self.fine.sent), (1, ["Now"]))


class Flaky:
    """Raises ``sqlite3.OperationalError`` the first time ``when(*args, **kwargs)`` is true, then calls through."""

    def __init__(self, real, when):
        self.real, self.when, self.raised = real, when, False

    def __call__(self, *args, **kwargs):
        if not self.raised and self.when(*args, **kwargs):
            self.raised = True
            raise sqlite3.OperationalError("database is locked")
        return self.real(*args, **kwargs)


class StoreErrorsInTheTick(unittest.TestCase):
    """The guard is for a plug-in's odd exception, but it wraps the whole of ``_send``: the credential read before the
    plug-in is called, and the write of its answer after. A failure there is the store's, not the plug-in's; it says
    nothing about the send. On main it stopped the tick and left the post 'publishing', for recovery to settle with the
    platform's own answer (``find``); the guard marks it 'unconfirmed' instead, which nothing ever revisits."""

    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = Findable()
        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
        self.publisher = Publisher(self.store, {self.platform.id: self.platform})
        prepared = self.platform.prepare({"title": "t"}, "Body.")
        self.post = self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title="t",
                                           status="scheduled", run_at=time.time() - 1, payload=prepared.to_dict())

    def tick(self, now: float | None = None) -> None:
        try:
            asyncio.run(self.publisher.run_due(now=now))
        except sqlite3.OperationalError:
            pass  # the tick stopped on the database error: that is what it should do

    def recover(self) -> None:
        """Recovery's next pass, once the post looks stuck: it asks the platform, then sends what is missing."""
        self.tick(now=time.time() + STUCK_AFTER + 1)

    def test_a_failed_write_after_a_confirmed_send_does_not_turn_the_post_unconfirmed(self):
        """The platform answered with its URL, then recording it failed. The guard marked the post 'unconfirmed'
        ("may or may not have been published") and dropped the answer."""
        flaky = Flaky(self.store.update_post, lambda *a, **kw: kw.get("status") == "published")
        with mock.patch.object(self.store, "update_post", flaky):
            self.tick()
        self.recover()
        post = self.store.get_post(self.post)
        self.assertEqual((post["status"], post["result_url"], post["remote_id"], self.platform.sent),
                         ("published", "https://findable/1", "1", ["t"]))

    def test_a_failed_read_before_the_send_does_not_strand_a_post_that_never_went_out(self):
        """Reading the credentials failed, so nothing was sent. The guard marked the post 'unconfirmed', so it is
        never sent, and the person is told it may already be out."""
        flaky = Flaky(self.store.get_credentials, lambda *a, **kw: True)
        with mock.patch.object(self.store, "get_credentials", flaky):
            self.tick()
        self.assertEqual(self.platform.sent, [])
        self.recover()
        post = self.store.get_post(self.post)
        self.assertEqual((post["status"], self.platform.sent), ("published", ["t"]))

    def test_credentials_that_cannot_be_unsealed_do_not_strand_every_due_post(self):
        """A scheduler started with the wrong INKCAST_SECRET_KEY can't unseal anyone's credentials, so nothing is
        sent. The guard marked every due post 'unconfirmed' instead, telling each person it may already be out, and
        nothing sends them. On main the tick stopped (the scheduler alert) and, once the key was put right,
        recovery found them missing and sent them."""
        path = tempfile.mkdtemp() + "/inkcast.db"
        store = Store(path, "k" * 40)
        self.addCleanup(store.close)
        user = store.upsert_user("local:on-call", "on-call", None, None)["id"]
        store.save_credentials(user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
        prepared = self.platform.prepare({"title": "t"}, "Body.")
        post_id = store.create_post(group_id="g", user_id=user, platform=self.platform.id, title="t",
                                    status="scheduled", run_at=time.time() - 1, payload=prepared.to_dict())
        wrong = Store(path, "w" * 40)
        self.addCleanup(wrong.close)
        try:
            asyncio.run(Publisher(wrong, {self.platform.id: self.platform}).run_due())
        except RuntimeError:
            pass  # the tick stopped: the credentials can't be read
        self.assertEqual(self.platform.sent, [])
        asyncio.run(Publisher(store, {self.platform.id: self.platform}).run_due(now=time.time() + STUCK_AFTER + 1))
        self.assertEqual((store.get_post(post_id)["status"], self.platform.sent), ("published", ["t"]))


if __name__ == "__main__":
    unittest.main()
