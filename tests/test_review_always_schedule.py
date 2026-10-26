# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #217 (issue #205, ADR-0015): a Git file with a ``publish_at`` is a scheduled post at the
time as written, and nothing is sent before that time; only a file with no ``publish_at`` is a "now" post.

The clock can't be moved in a test, so time passing is played by moving a post's ``run_at`` (and ``created_at``)
and by giving the scheduler's tick its clock (``run_due(now=...)``). Each ``Finding`` test failed on the PR head
before the fix committed with it."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.libs.config import RetryPolicy
from inkcast.libs.contracts.models import SyncFile, SyncRequest, SyncResult
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Destination, Platform, PlatformError, Prepared, PublishResult
from inkcast.services.publishing.sync import GitSync

SOURCE = "github.com/acme/blog"
PATH = "posts/review-205.md"


class Pubs(Platform):
    """Counts the sends. Posts go to a publication, resolved over the network before a post row is made. ``error``
    answers a send with that error."""

    id, name, default_api_url, credential_fields = "pubs", "Pubs", "https://pubs.invalid", []
    destination_label = "publication"

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sends = 0
        self.error: PlatformError | None = None

    async def verify(self, creds):
        return Account(id="1", name="pubs")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={}, destination="team")

    async def resolve_destination(self, creds, account, wanted):
        return Destination(id="team", name="Team")

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        if self.error is not None:
            raise self.error
        return PublishResult(url=f"https://pubs/{self.sends}", remote_id=str(self.sends), status="published")


def markdown(publish_at: datetime | None, body: str = "Body.") -> str:
    when = f"publish_at: {publish_at.isoformat()}\n" if publish_at else ""
    return f"---\ntitle: Review\nplatforms: [pubs]\n{when}---\n\n{body}\n"


def at(seconds_from_now: float) -> datetime:
    """A time that many seconds from now, to the second (as front matter is written)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).replace(microsecond=0)


class Review(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = Pubs()
        self.store.save_credentials(self.user, "pubs", {"k": "v"}, {"id": "1", "name": "on-call"})
        self.publisher = Publisher(self.store, {"pubs": self.platform}, retry=RetryPolicy(4, (60, 300, 1800)))
        self.git = GitSync(self.store, {"pubs": self.platform}, self.publisher)

    def sync(self, publish_at: datetime | None, body: str = "Body.", path: str = PATH, user: str | None = None
             ) -> SyncResult:
        req = SyncRequest(source=SOURCE, files=[SyncFile(path=path, markdown=markdown(publish_at, body))])
        (result,) = asyncio.run(self.git.sync(user or self.user, req)).results
        return result

    def posts(self) -> list[dict]:
        """In the order they were made (a replacement can keep the creation time of the post it replaces)."""
        return [dict(r) for r in self.store._all("SELECT * FROM posts ORDER BY rowid")]


class FindingARetryWaitNeverSendsBeforeANewTime(Review):
    def test_a_time_moved_past_a_pending_retry_inside_the_window_waits_for_it(self):
        """The file's time passed and its send hit a 429: the post waits for its retry, 60 s away. The author then
        dates it 100 s ahead: later than the retry, but under SCHEDULE_LEAD away, so the sync read it as unchanged,
        and the retry sent the post 40 s before the time the file asks for."""
        created = self.sync(at(-600))
        self.platform.error = PlatformError("Pubs rate limit hit; try again shortly.", retryable=True)
        asyncio.run(self.publisher.run_due())
        waiting = self.store.get_post(created.post_id)
        self.assertEqual((waiting["status"], waiting["attempts"], self.platform.sends), ("scheduled", 1, 1))
        self.platform.error = None

        later = at(100)
        self.assertGreater(later.timestamp(), waiting["run_at"] + 1, "the new time is after the retry")
        self.sync(later)
        asyncio.run(self.publisher.run_due(now=waiting["run_at"] + 1))       # the retry's time, before the file's
        self.assertEqual(self.platform.sends, 1, "sent at the retry, before the time the file asks for")
        asyncio.run(self.publisher.run_due(now=later.timestamp() + 1))
        self.assertEqual(self.platform.sends, 2)
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "published"], "one send, no twin")


if __name__ == "__main__":
    unittest.main()
