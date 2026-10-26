# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #205 (ADR-0015): a Git file with a ``publish_at`` is always a scheduled post, at the time as written.

Git publishing read a ``publish_at`` under 2 minutes away, or passed, as "now": a new file, or a time brought
forward into that window, was sent at once, up to 2 minutes before the time it asked for, as a "now" post the
scheduler never retries (a 429 left it ``failed`` until the next push). Now any file with a ``publish_at`` becomes
a scheduled post at the written time: a passed time is due at once, and the scheduler sends it at its next tick,
with retries under the policy and the 429's Retry-After. Only a file with no ``publish_at`` is a "now" post. MCP,
REST and the CLI keep refusing a past or near time (omit it to publish now).

The clock can't be moved in a test, so a time "in the window" or "passed" is written as such, and the scheduler's
tick is given its clock (``run_due(now=...)``). Each ``Finding`` test failed before the fix committed with it."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.libs.config import RetryPolicy
from inkcast.libs.content.story import StoryError, parse_story
from inkcast.libs.contracts.models import SyncFile, SyncRequest, SyncResult
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.services.publishing.sync import GitSync

SOURCE = "github.com/acme/blog"
PATH = "posts/dated.md"


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
    return f"---\ntitle: Dated\nplatforms: [counting]\n{when}---\n\n{body}\n"


def at(seconds_from_now: float) -> datetime:
    """A time that many seconds from now, to the second (as front matter is written)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).replace(microsecond=0)


class Dated(unittest.TestCase):
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

    def posts(self) -> list[dict]:
        return [dict(r) for r in self.store._all("SELECT * FROM posts ORDER BY created_at")]

    def assert_scheduled_at(self, result: SyncResult, when: datetime, action: str) -> dict:
        """The sync made a scheduled post at the written time and sent nothing itself."""
        self.assertEqual((result.action, result.status, self.platform.sends), (action, "scheduled", 0))
        post = self.store.get_post(result.post_id)
        self.assertEqual((post["status"], post["attempts"]), ("scheduled", 0))
        self.assertAlmostEqual(post["run_at"], when.timestamp(), delta=1, msg="the time as written")
        return post


class FindingAFileWithATimeIsAlwaysScheduled(Dated):
    def test_a_new_file_under_two_minutes_away_waits_for_its_time(self):
        """A new file dated 90 s ahead read as "now" and went out at once, 90 s early, as a post never retried."""
        when = at(90)
        created = self.assert_scheduled_at(self.sync(when), when, "create")
        self.assertEqual(self.sync(when).action, "unchanged")
        asyncio.run(self.publisher.run_due(now=when.timestamp() - 1))
        self.assertEqual(self.platform.sends, 0, "not due yet")
        asyncio.run(self.publisher.run_due(now=when.timestamp() + 1))
        self.assertEqual((self.platform.sends, self.store.get_post(created["id"])["status"]), (1, "published"))
        self.assertEqual(len(self.posts()), 1)

    def test_a_new_file_with_a_past_time_is_due_at_once_as_a_scheduled_post(self):
        """A file dated an hour ago (re-read long after its date): a scheduled post, due at once, that the
        scheduler sends at its next tick, not the sync."""
        when = at(-3600)
        created = self.assert_scheduled_at(self.sync(when), when, "create")
        self.assertEqual(self.sync(when).action, "unchanged", "unchanged whatever the clock")
        asyncio.run(self.publisher.run_due())
        self.assertEqual((self.platform.sends, self.store.get_post(created["id"])["status"]), (1, "published"))
        again = self.sync(when)
        self.assertEqual((again.action, again.status, self.platform.sends), ("unchanged", "published", 1))

    def test_a_time_brought_forward_into_the_window_waits_for_it(self):
        """Scheduled two days ahead, then dated 90 s ahead: the post was cancelled and sent at once, 90 s early.
        Now the replacement is scheduled at the new time, and nothing goes out before it."""
        created = self.sync(at(2 * 24 * 3600))
        self.assertEqual((created.action, created.status), ("create", "scheduled"))
        when = at(90)
        moved = self.assert_scheduled_at(self.sync(when), when, "update")
        self.assertNotEqual(moved["id"], created.post_id)
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")
        self.assertEqual(self.sync(when).action, "unchanged")
        asyncio.run(self.publisher.run_due(now=when.timestamp() - 1))
        self.assertEqual(self.platform.sends, 0, "not due yet")
        asyncio.run(self.publisher.run_due(now=when.timestamp() + 1))
        self.assertEqual((self.platform.sends, self.store.get_post(moved["id"])["status"]), (1, "published"))
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "published"], "one send, no twin")

    def test_a_time_moved_into_the_past_goes_out_on_the_next_tick_as_a_scheduled_post(self):
        """The author wants it out and dates it an hour ago: the replacement is due at once, and the scheduler
        sends it (with retries), rather than the sync sending a "now" post."""
        created = self.sync(at(2 * 24 * 3600))
        when = at(-3600)
        moved = self.assert_scheduled_at(self.sync(when), when, "update")
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")
        asyncio.run(self.publisher.run_due())
        self.assertEqual((self.platform.sends, self.store.get_post(moved["id"])["status"]), (1, "published"))

    def test_a_429_at_a_past_time_is_retried_under_the_policy_and_retry_after(self):
        """The issue's case: a dated file whose send is rate-limited. As a "now" post it was left failed until the
        next push; as a scheduled post it waits the longer of the policy's delay and the 429's ask, and is sent."""
        when = at(-3600)
        created = self.assert_scheduled_at(self.sync(when), when, "create")
        self.platform.error = PlatformError("Counting rate limit hit; try again shortly.", retryable=True, retry_after=90)
        asyncio.run(self.publisher.run_due())
        post = self.store.get_post(created["id"])
        self.assertEqual((post["status"], post["attempts"], self.platform.sends), ("scheduled", 1, 1))
        self.assertAlmostEqual(post["run_at"] - post["updated_at"], 90, delta=2, msg="the 429's ask, over the policy's 60 s")
        self.assertEqual(self.sync(when).action, "unchanged", "a CI run during the wait leaves it to the scheduler")
        self.platform.error = None
        asyncio.run(self.publisher.run_due(now=post["run_at"] + 1))
        self.assertEqual((self.platform.sends, self.store.get_post(created["id"])["status"]), (2, "published"))
        self.assertEqual([p["status"] for p in self.posts()], ["published"], "one post row, never a 'now' twin")

    def test_a_dry_run_of_a_new_dated_file_says_it_would_be_scheduled(self):
        for when in (at(90), at(-3600)):
            with self.subTest(when=when):
                self.setUp()
                result = self.sync(when, dry_run=True)
                self.assertEqual((result.action, result.status, self.platform.sends), ("create", "scheduled", 0))
                self.assertEqual(self.posts(), [])


class HeldWhatTheRuleKeeps(Dated):
    def test_a_file_with_no_time_is_still_a_now_post(self):
        created = self.sync(None)
        self.assertEqual((created.action, created.status, self.platform.sends), ("create", "published", 1))

    def test_a_removed_time_still_sends_now(self):
        created = self.sync(at(2 * 24 * 3600))
        self.assertEqual((created.action, created.status, self.platform.sends), ("create", "scheduled", 0))
        moved = self.sync(None)
        self.assertEqual((moved.action, moved.status, self.platform.sends), ("update", "published", 1))
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")

    def test_mcp_rest_and_the_cli_still_refuse_a_past_or_near_time(self):
        """``parse_when``'s meaning for a person's publish is unchanged: omit the time to publish now."""
        for when in (at(90), at(-3600)):
            with self.subTest(when=when):
                with self.assertRaisesRegex(StoryError, "past or under 2 minutes away"):
                    parse_story(markdown(when), known_platforms=["counting"])
                story = parse_story(markdown(when), known_platforms=["counting"], schedule_as_written=True)
                self.assertEqual(story.publish_at, when, "Git publishing keeps the time as written")
        far = at(5 * 24 * 3600)
        for kwargs in ({}, {"schedule_as_written": True}):
            self.assertEqual(parse_story(markdown(far), known_platforms=["counting"], **kwargs).publish_at, far)


if __name__ == "__main__":
    unittest.main()
