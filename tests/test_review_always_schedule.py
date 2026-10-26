# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #217 (issue #205, ADR-0015): a Git file with a ``publish_at`` is a scheduled post at the
time as written, and nothing is sent before that time; only a file with no ``publish_at`` is a "now" post.

The clock can't be moved in a test, so time passing is played by moving a post's ``run_at`` (and ``created_at``)
and by giving the scheduler's tick its clock (``run_due(now=...)``). Each ``Finding`` test failed on the PR head
before the fix committed with it."""

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.libs.config import Config, RetryPolicy
from inkcast.libs.contracts.models import SyncFile, SyncRequest, SyncResult
from inkcast.libs.db import Store
from inkcast.libs.db.ledger import CLAIM_TTL
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Destination, Platform, PlatformError, Prepared, PublishResult
from inkcast.libs.runtime import build_metrics
from inkcast.services.publishing.sync import GitSync

SOURCE = "github.com/acme/blog"
PATH = "posts/review-205.md"


class Stopped(BaseException):
    """The process stopped right here (OOM kill, node drain, deploy): nothing after this line runs."""


class Pubs(Platform):
    """Counts the sends. Posts go to a publication, resolved over the network before a post row is made. ``error``
    answers a send with that error; ``stop`` stops the process during that lookup."""

    id, name, default_api_url, credential_fields = "pubs", "Pubs", "https://pubs.invalid", []
    destination_label = "publication"

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sends = 0
        self.error: PlatformError | None = None
        self.stop = False

    async def verify(self, creds):
        return Account(id="1", name="pubs")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={}, destination="team")

    async def resolve_destination(self, creds, account, wanted):
        if self.stop:
            raise Stopped()
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


class FindingAReplacementCutOffIsNotLost(Review):
    """The sync cancels a scheduled post, then makes its replacement. Stopped in between (the publication lookup is a
    network call), the ledger's claim still named the cancelled post. Once the claim expired the sync followed that
    post as the claim's own: cancelled, so the file read as unchanged, and the post was never sent."""

    def cut_off(self, publish_at: datetime | None, body: str = "Body.") -> None:
        self.platform.stop = True
        with self.assertRaises(Stopped):
            self.sync(publish_at, body)
        self.platform.stop = False

    def claim_expires(self) -> None:
        self.store._exec("UPDATE ledger SET synced_at = synced_at - ?", CLAIM_TTL)

    def test_a_rescheduled_post_cut_off_mid_replacement_is_made_once_the_claim_expires(self):
        created = self.sync(at(2 * 24 * 3600))
        later = at(3 * 24 * 3600)
        self.cut_off(later)
        self.assertEqual(self.store.get_post(created.post_id)["status"], "cancelled")
        self.assertEqual(self.sync(later).action, "skipped", "the claim still holds")
        self.claim_expires()
        rerun = self.sync(later)
        live = [p for p in self.posts() if p["status"] == "scheduled"]
        self.assertEqual(len(live), 1, f"cancelled by the sync and never made again ({rerun.action}): lost")
        self.assertAlmostEqual(live[0]["run_at"], later.timestamp(), delta=1, msg="at the time as written")
        self.assertEqual((rerun.action, rerun.status, rerun.post_id), ("create", "scheduled", live[0]["id"]))
        self.assertEqual(self.sync(later).action, "unchanged")
        asyncio.run(self.publisher.run_due(now=later.timestamp() + 1))
        self.assertEqual((self.platform.sends, self.store.get_post(live[0]["id"])["status"]), (1, "published"))

    def test_a_removed_time_cut_off_mid_replacement_is_sent_once_the_claim_expires(self):
        self.sync(at(2 * 24 * 3600))
        self.cut_off(None)
        self.claim_expires()
        rerun = self.sync(None)
        self.assertEqual((rerun.status, self.platform.sends), ("published", 1), "the author asked for it now: lost")

    def test_a_cancelled_post_the_author_edits_again_is_made_once_a_cut_off_claim_expires(self):
        """The same on the create path: a person cancelled the post, the author edits the file to have it sent after
        all, and the sync is stopped making it. The claim named the cancelled post, and its hash is the new file's."""
        when = at(2 * 24 * 3600)
        created = self.sync(when)
        self.publisher.cancel(self.user, created.post_id)
        self.cut_off(when, body="Better body.")
        self.claim_expires()
        rerun = self.sync(when, body="Better body.")
        self.assertEqual((rerun.action, rerun.status), ("create", "scheduled"), "the author's edit was lost")

    def test_a_body_edit_cut_off_mid_replacement_is_still_made(self):
        """What the fix keeps: a changed body was re-made before too (its hash differs from the claim's)."""
        when = at(2 * 24 * 3600)
        self.sync(when)
        self.cut_off(when, body="Better body.")
        self.claim_expires()
        rerun = self.sync(when, body="Better body.")
        self.assertEqual((rerun.action, rerun.status), ("create", "scheduled"))
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "scheduled"], "one post, no twin")


class Measured(Review):
    def setUp(self):
        super().setUp()
        config = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:")
        self.publisher.metrics = build_metrics(self.store, {"pubs": self.platform}, config)

    def behind(self, post_id: str, seconds: float) -> datetime:
        """The post was scheduled a day ahead; its time passed ``seconds`` ago and the scheduler has not sent it."""
        when = at(-seconds)
        self.store._exec("UPDATE posts SET run_at = ?, created_at = ? WHERE id = ?", when.timestamp(),
                         when.timestamp() - 86400, post_id)
        return when

    def lag_of_the_send(self) -> float:
        (published,) = [e for e in self.store.poll("review") if e.type == "post.published"]
        self.assertTrue(published.attrs["scheduled"])
        return published.attrs["lag_seconds"]


class FindingAnEditDoesNotHideHowLateAPostIs(Measured):
    def test_a_body_edit_while_the_scheduler_is_behind_keeps_the_post_late(self):
        """A post scheduled a day ahead, whose time passed 5 minutes ago with the scheduler behind. The author fixes a
        typo: the replacement keeps the time, but is a new row created now, so it read as on time (lag 0, not
        overdue) where main read it 5 minutes late. Edits more often than INKCAST_OVERDUE_GRACE keep
        InkcastPostsOverdue quiet however far behind the scheduler is."""
        created = self.sync(at(2 * 24 * 3600))
        when = self.behind(created.post_id, 300)
        self.assertEqual(self.store.overdue_count(time.time(), 60), 1, "5 minutes late before the edit")
        edited = self.sync(when, body="Better body.")
        self.assertEqual((edited.action, edited.status, self.platform.sends), ("update", "scheduled", 0))
        now = time.time()
        self.assertEqual(self.store.overdue_count(now, 60), 1, "the edit made a 5-minute-late post look on time")
        self.assertGreaterEqual(self.store.oldest_due_age(now), 300)
        asyncio.run(self.publisher.run_due(now=now))
        self.assertIn('inkcast_publish_lag_seconds_bucket{platform="pubs",le="120"} 0', self.publisher.metrics.render(),
                      "counted on time by the schedule-timeliness SLO")
        self.assertGreaterEqual(self.lag_of_the_send(), 300, "counted on time in the customer's on-time rate")

    def test_a_past_dated_file_edited_before_its_first_tick_is_still_on_time(self):
        """What the fix keeps (ADR-0015): a file dated a week ago is due from when it was first known, edits or not."""
        week_ago = at(-7 * 24 * 3600)
        created = self.sync(week_ago)
        edited = self.sync(week_ago, body="Better body.")
        self.assertEqual((edited.action, edited.status), ("update", "scheduled"))
        self.assertNotEqual(edited.post_id, created.post_id)
        now = time.time()
        self.assertEqual(self.store.overdue_count(now, 60), 0)
        asyncio.run(self.publisher.run_due(now=now))
        self.assertLess(self.lag_of_the_send(), 60)

    def test_a_time_moved_into_the_past_is_due_from_the_move(self):
        """What the fix keeps: a time the author moves into the past is due from that sync, not from the time."""
        self.sync(at(2 * 24 * 3600))
        self.sync(at(-3600))
        asyncio.run(self.publisher.run_due())
        self.assertLess(self.lag_of_the_send(), 60)


if __name__ == "__main__":
    unittest.main()
