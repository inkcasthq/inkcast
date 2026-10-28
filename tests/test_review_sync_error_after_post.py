# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #223 (issue #213): once the post row exists, an error from ``publish()`` leaves the ledger
entry on that post, with the post's own status; only an error before any row marks it 'failed'.

Each error is injected on the one write it is about (``Flaky``, as in ``tests/test_sync_error_after_post.py``), so
every case is deterministic. The clock can't be moved, so the scheduler's recovery pass is given its clock
(``run_due(now=...)``). Each ``Finding`` test failed on the PR head before the fix committed with it."""

import asyncio
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx2

from inkcast.libs.contracts.models import SyncFile, SyncRequest, SyncResult
from inkcast.libs.db import Store
from inkcast.libs.db.ledger import CLAIM_TTL
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.services.publishing.sync import GitSync

SOURCE = "github.com/acme/blog"
PATH = "posts/review-213.md"


class Blind(Platform):
    """Counts the sends; can't be asked whether a post exists (no ``find``, like Medium). ``error`` answers a send
    with that error instead."""

    id, name, default_api_url, credential_fields = "blind", "Blind", "https://blind.invalid", []

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sent: list[str] = []
        self.error: PlatformError | None = None

    async def verify(self, creds):
        return Account(id="1", name=self.id)

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sent.append(prepared.payload["body"].strip())
        if self.error is not None:
            raise self.error
        n = len(self.sent)
        return PublishResult(url=f"https://{self.id}/{n}", remote_id=str(n), status="published")


class Findable(Blind):
    """The same platform with ``find`` (like DEV): an interrupted send can be checked."""

    id, name, default_api_url = "findable", "Findable", "https://findable.invalid"
    capabilities = frozenset({"find"})

    async def find_existing(self, creds, account, prepared):
        body = prepared.payload["body"].strip()
        if body in self.sent:
            n = self.sent.index(body) + 1
            return PublishResult(url=f"https://{self.id}/{n}", remote_id=str(n), status="published")
        return None


class Flaky:
    """Raises ``sqlite3.OperationalError`` ("database is locked") the first ``times`` calls for which
    ``when(*args, **kwargs)`` is true, then calls through: a busy writer elsewhere holds the lock that long."""

    def __init__(self, real, when, times: int = 1):
        self.real, self.when, self.left, self.raised = real, when, times, 0

    def __call__(self, *args, **kwargs):
        if self.left and self.when(*args, **kwargs):
            self.left -= 1
            self.raised += 1
            raise sqlite3.OperationalError("database is locked")
        return self.real(*args, **kwargs)


def markdown(platform: str, publish_at: datetime | None = None, body: str = "Body.") -> str:
    when = f"publish_at: {publish_at.isoformat()}\n" if publish_at else ""
    return f"---\ntitle: Review\nplatforms: [{platform}]\n{when}---\n\n{body}\n"


def at(seconds_from_now: float) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).replace(microsecond=0)


def the_post_pointer(*args, **kwargs) -> bool:
    """The ledger write that names the post just made, before anything is sent (``on_created``)."""
    return set(kwargs) == {"post_id", "content_hash"}


class Review(unittest.TestCase):
    platform_class: type[Blind] = Blind

    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = self.platform_class()
        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "on-call"})
        self.publisher = Publisher(self.store, {self.platform.id: self.platform})
        self.git = GitSync(self.store, {self.platform.id: self.platform}, self.publisher)

    def sync(self, publish_at: datetime | None = None, body: str = "Body.") -> SyncResult:
        req = SyncRequest(source=SOURCE, files=[SyncFile(path=PATH, markdown=markdown(self.platform.id, publish_at, body))])
        (result,) = asyncio.run(self.git.sync(self.user, req)).results
        return result

    def sync_locked(self, locked: Flaky, method: str, publish_at: datetime | None = None, body: str = "Body.") -> None:
        """A sync during which ``method`` of the store is ``locked``: it raises the database error."""
        with mock.patch.object(self.store, method, locked), self.assertRaises(sqlite3.OperationalError):
            self.sync(publish_at, body)
        self.assertTrue(locked.raised, f"the locked {method} was never reached")

    def entry(self) -> dict:
        return self.store.get_entry(self.user, SOURCE, PATH, self.platform.id)

    def posts(self) -> list[dict]:
        return [dict(r) for r in self.store._all("SELECT * FROM posts ORDER BY rowid")]

    def tick(self, later: float = STUCK_AFTER + 1) -> None:
        """A scheduler pass ``later`` seconds from now: it recovers what looks stuck, and sends what is due."""
        asyncio.run(self.publisher.run_due(now=time.time() + later))

    def settle(self, publish_at: datetime | None = None, body: str = "Body.") -> None:
        """What happens next without anyone stepping in: the scheduler's passes and the next CI runs."""
        for _ in range(3):
            self.tick()
            self.sync(publish_at, body)
        self.tick()


class FindingAPostNeverSentIsNotLeftUnconfirmed(Review):
    """The ledger write naming the post (``on_created``, before the send) hit a locked database: the row exists and
    ``publish()`` stopped there, so nothing was sent, and the sync knows it. The PR left the post 'publishing' for
    recovery, which can only ask the platform. One that can't be asked (Medium) gets it marked unconfirmed, the entry
    follows, and nothing sends it: the post is lost (main sent it once, at the next sync)."""

    def test_a_now_post_whose_pointer_write_was_locked_is_sent_once(self):
        self.sync_locked(Flaky(self.store.put_entry, the_post_pointer), "put_entry")
        self.assertEqual(self.platform.sent, [])
        self.settle()
        self.assertEqual(self.platform.sent, ["Body."], "never sent: left unconfirmed on a platform that can't be asked")
        self.assertEqual(self.sync().action, "unchanged")

    def test_a_now_replacement_whose_pointer_write_was_locked_is_sent_once(self):
        """The same mid-replacement: the scheduled post was cancelled for it, so losing it loses the story."""
        self.sync(at(2 * 24 * 3600))
        self.sync_locked(Flaky(self.store.put_entry, the_post_pointer), "put_entry")
        self.settle()
        self.assertEqual(self.platform.sent, ["Body."], "the old post cancelled, its replacement never sent")


class KeptAPostNeverSentIsSentOnceWhereItCanBeAsked(FindingAPostNeverSentIsNotLeftUnconfirmed):
    """What the fix keeps: on a platform that can be asked, the post is sent once too, never twice."""

    platform_class = Findable


class FindingALockOutlastingTheRetryDoesNotSendTwice(Review):
    """The PR's fallback when ``_after_error``'s own write is locked too: "the entry stays the claim naming the post,
    which an expired claim already follows". Not when the first locked write was the one naming the post: the claim
    names none, the row stays, and once the claim expires the next sync takes it over and makes a second post. One
    writer holding the lock past two busy timeouts sends the file twice, the issue's own symptom."""

    platform_class = Findable

    def locked_long(self, publish_at: datetime | None = None) -> None:
        """Two writes in a row are locked: the one naming the post, and the next (whichever it is)."""
        entries = Flaky(self.store.put_entry, lambda *a, **kw: True, times=2)
        posts = Flaky(self.store.update_post, lambda *a, **kw: True)
        with mock.patch.object(self.store, "update_post", posts):
            self.sync_locked(entries, "put_entry", publish_at)
        self.assertEqual(entries.raised + posts.raised, 2)
        self.store._exec("UPDATE ledger SET synced_at = synced_at - ?", CLAIM_TTL)  # the claim expires

    def test_a_now_post_is_sent_once(self):
        self.locked_long()
        self.settle()
        self.assertEqual(self.platform.sent, ["Body."], "recovery sent the first row, the next sync made a second")

    def test_a_scheduled_post_is_sent_once(self):
        when = at(2 * 24 * 3600)
        self.locked_long(when)
        self.settle(when)
        asyncio.run(self.publisher.run_due(now=when.timestamp() + 1))
        self.assertEqual(self.platform.sent, ["Body."], "two posts scheduled for the one file")


if __name__ == "__main__":
    unittest.main()
