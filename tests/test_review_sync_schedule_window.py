# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #194 (issue #187): a Git sync near a scheduled post's time must not send it early, twice,
or never, and must not ignore a change the author made.

As in ``test_sync_schedule_window``, the clock can't be moved in a test, so time passing is played by moving the
post's ``run_at`` and the file's ``publish_at`` together to the same new value (the content hash doesn't include the
time). Each ``Finding`` test failed on the PR head before the fix committed with it."""

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
PATH = "posts/review-later.md"


class Publication(Platform):
    """Counts the sends. Posts go to a publication, resolved over the network before a post row is made: ``down``
    makes that lookup fail the way a network blip does."""

    id, name, default_api_url, credential_fields = "pubs", "Pubs", "https://pubs.invalid", []
    destination_label = "publication"

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sends = 0
        self.down = False

    async def verify(self, creds):
        return Account(id="1", name="pubs")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={}, destination="team")

    async def resolve_destination(self, creds, account, wanted):
        if self.down:
            raise PlatformError("Could not reach Pubs: connection reset", retryable=True)
        return Destination(id="team", name="Team")

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        return PublishResult(url=f"https://pubs/{self.sends}", remote_id=str(self.sends), status="published")


def markdown(publish_at: datetime | None, body: str = "Body.") -> str:
    when = f"publish_at: {publish_at.isoformat()}\n" if publish_at else ""
    return f"---\ntitle: Later\nplatforms: [pubs]\n{when}---\n\n{body}\n"


def at(seconds_from_now: float) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).replace(microsecond=0)


class Window(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = Publication()
        self.store.save_credentials(self.user, "pubs", {"k": "v"}, {"id": "1", "name": "on-call"})
        self.publisher = Publisher(self.store, {"pubs": self.platform}, retry=RetryPolicy(4, (60, 300, 1800)))
        self.git = GitSync(self.store, {"pubs": self.platform}, self.publisher)

    def sync(self, publish_at: datetime | None, body: str = "Body.") -> SyncResult:
        req = SyncRequest(source=SOURCE, files=[SyncFile(path=PATH, markdown=markdown(publish_at, body))])
        (result,) = asyncio.run(self.git.sync(self.user, req)).results
        return result

    def scheduled(self) -> SyncResult:
        created = self.sync(at(2 * 24 * 3600))
        self.assertEqual((created.action, created.status, self.platform.sends), ("create", "scheduled", 0))
        return created

    def clock_reaches(self, post_id: str, seconds_before: float) -> datetime:
        when = at(seconds_before)
        self.store._exec("UPDATE posts SET run_at = ? WHERE id = ?", when.timestamp(), post_id)
        return when

    def posts(self) -> list[dict]:
        return [dict(r) for r in self.store._all("SELECT * FROM posts ORDER BY created_at")]


class FindingARetriedReplacementKeepsTheTimeItKept(Window):
    def test_a_rerun_after_a_failed_replacement_inside_the_window_does_not_send_early(self):
        """A body edit 90 s before the time: the sync cancels the scheduled post and its replacement, which #194 keeps
        at the file's time, fails to be made (the publication lookup hit a network blip). CI runs the same commit
        again 30 s later. The file's schedule never changed, yet the re-run read it as "now" and sent the post at
        once, a minute early, as a "now" post that is never retried."""
        created = self.scheduled()
        when = self.clock_reaches(created.post_id, 90)
        self.platform.down = True
        failed = self.sync(when, body="Better body.")
        self.assertEqual((failed.action, self.platform.sends), ("failed", 0))
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")

        self.platform.down = False
        rerun = self.sync(when, body="Better body.")
        self.assertEqual(self.platform.sends, 0, "sent at once, before its time, as a 'now' post")
        self.assertEqual(rerun.status, "scheduled")
        self.assertAlmostEqual(self.store.get_post(rerun.post_id)["run_at"], when.timestamp(), delta=1)
        again = self.sync(when, body="Better body.")                   # and it stays the scheduler's
        self.assertEqual((again.action, again.post_id, self.platform.sends), ("unchanged", rerun.post_id, 0))
        asyncio.run(self.publisher.run_due(now=when.timestamp() + 1))
        self.assertEqual((self.platform.sends, self.store.get_post(rerun.post_id)["status"]), (1, "published"))
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "published"], "one send, no twin")

    def test_a_person_cancelling_the_post_is_still_respected(self):
        """What the fix keeps: a post a person cancelled stays cancelled while the file is unchanged."""
        created = self.scheduled()
        when = self.clock_reaches(created.post_id, 90)
        self.publisher.cancel(self.user, created.post_id)
        again = self.sync(when)
        self.assertEqual((again.action, again.status, self.platform.sends), ("unchanged", "cancelled", 0))
        self.assertEqual(len(self.posts()), 1)


if __name__ == "__main__":
    unittest.main()
