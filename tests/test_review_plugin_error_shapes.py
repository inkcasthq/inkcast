# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #189 (issue #174): the per-post guard against a plug-in's odd exception, and where it sits.

Each test failed on the PR's head before the fix committed with it."""

import asyncio
import unittest

import httpx2

from inkcast.libs.content.story import parse_story
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
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


if __name__ == "__main__":
    unittest.main()
