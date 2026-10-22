# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #124 (issue #89, F1.10.S1): the publish retry policy as settings.

Each test failed on the PR head before the fix committed with it."""

import asyncio
import pathlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.libs.config import RetryPolicy
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared
from inkcast.services.publishing.sync import GitSync

SOURCE = "github.com/acme/blog"
ROOT = pathlib.Path(__file__).resolve().parent.parent


class RateLimited(Platform):
    """Answers every publish with a 429 (the send certainly never happened), and counts the sends."""

    id, name, default_api_url, credential_fields = "limited", "Limited", "https://limited.invalid", []

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sends = 0

    async def verify(self, creds):
        return Account(id="1", name="limited")

    def prepare(self, fields, body):
        return Prepared(platform="limited", title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        raise PlatformError("Limited rate limit hit; try again shortly.", retryable=True)


def story(publish_at: datetime) -> str:
    return (f"---\ntitle: Later\nplatforms: [limited]\npublish_at: {publish_at.replace(microsecond=0).isoformat()}\n"
            "---\n\nBody.\n")


class GitSyncFollowsTheRetryPolicy(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:usr", "usr", None, None)["id"]
        self.platform = RateLimited()
        self.store.save_credentials(self.user, "limited", {"k": "v"}, {"id": "1", "name": "limited"})
        # An operator gives this platform room: four sends, six hours apart (a 429 means "slow down").
        self.publisher = Publisher(self.store, {"limited": self.platform}, retry=RetryPolicy(4, (21600,)))
        self.git = GitSync(self.store, {"limited": self.platform}, self.publisher)

    def sync(self, markdown: str):
        req = SyncRequest(source=SOURCE, files=[SyncFile(path="posts/later.md", markdown=markdown)])
        (result,) = asyncio.run(self.git.sync(self.user, req)).results
        return result

    def test_a_sync_during_a_retry_wait_leaves_the_post_to_the_scheduler(self):
        """A Git-published post whose send got a 429 waits INKCAST_PUBLISH_RETRY_DELAYS for its next try. Any CI run
        in that wait syncs the same file, whose publish_at is now in the past ("now" for Git publishing): the sync took
        the retry's run_at for a schedule change, cancelled the post and sent a new one at once. So the policy was
        never followed for Git-published posts: the next send came hours early, as a single send with no retry
        (a 'failed' post, which the next sync creates and sends again), and the attempt count started over."""
        due = datetime.now(timezone.utc) + timedelta(days=2)
        created = self.sync(story(due))
        self.assertEqual((created.action, created.status), ("create", "scheduled"))
        # Its time comes: the scheduler sends it, the platform answers 429, and the post waits 6 h for its retry.
        self.store._exec("UPDATE posts SET run_at = ? WHERE id = ?", time.time() - 1, created.post_id)
        asyncio.run(self.publisher.run_due())
        post = self.store.get_post(created.post_id)
        self.assertEqual((self.platform.sends, post["status"], post["attempts"]), (1, "scheduled", 1))
        self.assertAlmostEqual(post["run_at"] - time.time(), 21600, delta=60)

        # A CI run an hour later: the same file, its publish_at now past (here written as the past it has become).
        again = self.sync(story(datetime.now(timezone.utc) - timedelta(hours=1)))
        self.assertEqual(self.platform.sends, 1, "the sync sent the post at once, hours before its retry")
        self.assertEqual((again.action, again.status, again.post_id), ("unchanged", "scheduled", created.post_id))
        post = self.store.get_post(created.post_id)
        self.assertEqual((post["status"], post["attempts"]), ("scheduled", 1))
        self.assertAlmostEqual(post["run_at"] - time.time(), 21600, delta=60)

    def test_a_new_date_in_the_file_still_reschedules_a_post_waiting_for_its_retry(self):
        """What the fix keeps: moving publish_at to a new future time is a change, retry or not."""
        created = self.sync(story(datetime.now(timezone.utc) + timedelta(days=2)))
        self.store._exec("UPDATE posts SET run_at = ? WHERE id = ?", time.time() - 1, created.post_id)
        asyncio.run(self.publisher.run_due())
        later = datetime.now(timezone.utc) + timedelta(days=5)
        moved = self.sync(story(later))
        self.assertEqual((moved.action, moved.status, self.platform.sends), ("update", "scheduled", 1))
        self.assertAlmostEqual(self.store.get_post(moved.post_id)["run_at"], later.replace(microsecond=0).timestamp(),
                               delta=1)
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")


def doc_section(path: str, heading: str) -> str:
    """The text under a ``## heading`` of a Markdown file, up to the next one."""
    text = (ROOT / path).read_text(encoding="utf-8")
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else None]


class DocsStateThePolicy(unittest.TestCase):
    def test_the_failure_rate_runbook_names_the_settings_not_the_old_fixed_delays(self):
        """docs/runbooks/alerts.md told the person on call, during a platform outage, to "watch retries (60 s, 5 m,
        30 m)": the constants this PR turned into settings. With any other INKCAST_PUBLISH_RETRY_DELAYS they wait for
        retries that never come at those times, or miss the ones that do."""
        section = doc_section("docs/runbooks/alerts.md", "InkcastPublishFailureRateHigh")
        self.assertIn("INKCAST_PUBLISH_MAX_ATTEMPTS", section)
        self.assertIn("INKCAST_PUBLISH_RETRY_DELAYS", section)


if __name__ == "__main__":
    unittest.main()
