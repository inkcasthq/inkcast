# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #187: a Git sync in the 2 minutes before a scheduled post's time must leave the post alone.

Git publishing reads a ``publish_at`` that has passed as "now" (files are re-read long after their date), and the
parser's "past" starts 2 minutes ahead. ``sync._one`` compared that reading with the scheduled post's time: inside
the window the file read as "now", which differed from the post's time, so an unchanged file cancelled its scheduled
post and sent a new "now" post at once. A "now" post is never retried by the scheduler: a 429 at that moment left
it failed, with the original scheduled post cancelled.

The time the file asks for is now carried as written, and a post the scheduler hasn't tried yet is compared with
that: an unchanged file is unchanged whatever the clock says. The "now" reading only applies when the file's schedule
actually changed. The clock can't be moved in a test, so time passing is played by moving the post's time and the
file's ``publish_at`` together to the same new value: that is exactly what the sync sees when the clock reaches the
window (the content hash doesn't include the time). Each ``Finding`` test failed on main before the fix committed
with it; the ``Held`` tests pin what the fix keeps."""

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.libs.config import RetryPolicy
from inkcast.libs.contracts.models import SyncFile, SyncRequest, SyncResult
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.services.publishing.sync import GitSync

SOURCE = "github.com/acme/blog"
PATH = "posts/later.md"


class Counting(Platform):
    """Counts the sends; answers each with the error it is told to, or success."""

    id, name, default_api_url, credential_fields = "counting", "Counting", "https://counting.invalid", []

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sends = 0
        self.error: PlatformError | None = None

    async def verify(self, creds):
        return Account(id="1", name="counting")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        if self.error is not None:
            raise self.error
        return PublishResult(url=f"https://counting/{self.sends}", remote_id=str(self.sends), status="published")


def markdown(publish_at: datetime | None, body: str = "Body.") -> str:
    when = f"publish_at: {publish_at.isoformat()}\n" if publish_at else ""
    return f"---\ntitle: Later\nplatforms: [counting]\n{when}---\n\n{body}\n"


def at(seconds_from_now: float) -> datetime:
    """A time that many seconds from now, to the second (as front matter is written)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).replace(microsecond=0)


class Window(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = Counting()
        self.store.save_credentials(self.user, "counting", {"k": "v"}, {"id": "1", "name": "on-call"})
        self.publisher = Publisher(self.store, {"counting": self.platform}, retry=RetryPolicy(4, (60, 300, 1800)))
        self.git = GitSync(self.store, {"counting": self.platform}, self.publisher)

    def sync(self, publish_at: datetime | None, body: str = "Body.", dry_run: bool = False) -> SyncResult:
        req = SyncRequest(source=SOURCE, files=[SyncFile(path=PATH, markdown=markdown(publish_at, body))], dry_run=dry_run)
        (result,) = asyncio.run(self.git.sync(self.user, req)).results
        return result

    def scheduled(self) -> SyncResult:
        """The file first synced two days ahead: a scheduled post the scheduler hasn't touched."""
        created = self.sync(at(2 * 24 * 3600))
        self.assertEqual((created.action, created.status, self.platform.sends), ("create", "scheduled", 0))
        return created

    def clock_reaches(self, post_id: str, seconds_before: float) -> datetime:
        """Time passes until ``seconds_before`` seconds before the post's time: the post's time and the file's
        ``publish_at`` are the same value, now that far ahead (negative: that far behind, the scheduler late)."""
        when = at(seconds_before)
        self.store._exec("UPDATE posts SET run_at = ? WHERE id = ?", when.timestamp(), post_id)
        return when

    def posts(self) -> list[dict]:
        return [dict(r) for r in self.store._all("SELECT * FROM posts ORDER BY created_at")]


class FindingAnUnchangedFileIsLeftAloneWhateverTheClock(Window):
    def test_a_sync_90_seconds_before_the_time_leaves_the_post_to_the_scheduler(self):
        created = self.scheduled()
        when = self.clock_reaches(created.post_id, 90)
        again = self.sync(when)
        self.assertEqual((again.action, again.status, again.post_id), ("unchanged", "scheduled", created.post_id))
        self.assertEqual(self.platform.sends, 0, "sent at once, 90 s early, as a 'now' post")
        post = self.store.get_post(created.post_id)
        self.assertEqual((post["status"], post["attempts"]), ("scheduled", 0))
        self.assertAlmostEqual(post["run_at"], when.timestamp(), delta=1)
        self.assertEqual(len(self.posts()), 1, "no second post row")
        # The scheduler sends it at its time, as scheduled.
        asyncio.run(self.publisher.run_due(now=when.timestamp() - 1))
        self.assertEqual(self.platform.sends, 0, "not due yet")
        asyncio.run(self.publisher.run_due(now=when.timestamp() + 1))
        self.assertEqual((self.platform.sends, self.store.get_post(created.post_id)["status"]), (1, "published"))

    def test_the_window_edge_and_a_sync_after_the_time_while_the_scheduler_is_behind(self):
        for seconds_before in (119, 60, 1, 0, -1, -60, -3600):
            with self.subTest(seconds_before=seconds_before):
                self.setUp()
                created = self.scheduled()
                when = self.clock_reaches(created.post_id, seconds_before)
                again = self.sync(when)
                self.assertEqual((again.action, again.post_id, self.platform.sends), ("unchanged", created.post_id, 0))
                self.assertEqual(self.store.get_post(created.post_id)["status"], "scheduled")
                asyncio.run(self.publisher.run_due(now=max(time.time(), when.timestamp()) + 1))
                self.assertEqual((self.platform.sends, self.store.get_post(created.post_id)["status"]), (1, "published"))

    def test_a_429_at_its_time_is_retried_under_the_policy_as_a_scheduled_post(self):
        """The probe in the issue: synced inside the window, the post went out as a "now" post, got a 429 and was
        left failed, with the scheduled post cancelled. Left alone, it is the scheduler's, and retried."""
        created = self.scheduled()
        when = self.clock_reaches(created.post_id, 90)
        self.sync(when)
        self.platform.error = PlatformError("Counting rate limit hit; try again shortly.", retryable=True, retry_after=5)
        asyncio.run(self.publisher.run_due(now=when.timestamp() + 1))
        post = self.store.get_post(created.post_id)
        self.assertEqual((post["status"], post["attempts"], self.platform.sends), ("scheduled", 1, 1))
        self.assertAlmostEqual(post["run_at"] - post["updated_at"], 60, delta=2, msg="the policy's first delay")
        # A CI run during the wait (the file's time is past now) still leaves it alone.
        self.assertEqual(self.sync(when).action, "unchanged")
        self.platform.error = None
        asyncio.run(self.publisher.run_due(now=post["run_at"] + 1))
        self.assertEqual((self.platform.sends, self.store.get_post(created.post_id)["status"]), (2, "published"))
        self.assertEqual([p["status"] for p in self.posts()], ["published"], "one post row, never a 'now' twin")

    def test_a_dry_run_inside_the_window_reports_unchanged(self):
        created = self.scheduled()
        when = self.clock_reaches(created.post_id, 90)
        self.assertEqual(self.sync(when, dry_run=True).action, "unchanged")
        self.assertEqual((self.platform.sends, self.store.get_post(created.post_id)["status"]), (0, "scheduled"))


class HeldAChangedScheduleStillApplies(Window):
    """A "now" reading of publish_at applies when the file's schedule actually changed, and so does a new time."""

    def test_a_time_moved_into_the_past_sends_now(self):
        created = self.scheduled()
        moved = self.sync(at(-3600))                                   # the author wants it out: dated an hour ago
        self.assertEqual((moved.action, moved.status, self.platform.sends), ("update", "published", 1))
        self.assertNotEqual(moved.post_id, created.post_id)
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")

    def test_a_time_moved_inside_the_window_sends_now(self):
        created = self.scheduled()
        moved = self.sync(at(90))                                      # changed to a time under 2 minutes away
        self.assertEqual((moved.action, moved.status, self.platform.sends), ("update", "published", 1))
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")

    def test_a_removed_time_sends_now(self):
        created = self.scheduled()
        moved = self.sync(None)
        self.assertEqual((moved.action, moved.status, self.platform.sends), ("update", "published", 1))
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")

    def test_a_new_future_time_reschedules(self):
        created = self.scheduled()
        later = at(5 * 24 * 3600)
        moved = self.sync(later)
        self.assertEqual((moved.action, moved.status, self.platform.sends), ("update", "scheduled", 0))
        self.assertAlmostEqual(self.store.get_post(moved.post_id)["run_at"], later.timestamp(), delta=1)
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")

    def test_a_changed_body_inside_the_window_reschedules_with_the_new_content_at_the_same_time(self):
        created = self.scheduled()
        when = self.clock_reaches(created.post_id, 90)
        edited = self.sync(when, body="Better body.")
        self.assertEqual((edited.action, self.platform.sends), ("update", 1), "a changed file 90 s before its time "
                         "goes out now, as any past or near time does for Git publishing")
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
