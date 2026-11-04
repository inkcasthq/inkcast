# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #222 (ADR-0019): the follow-ups to ADR-0015's "a publish_at is always a scheduled post".

ADR-0015 measured a post's lag, the overdue count and the oldest-due age from the later of its time and its creation,
and claimed posts in that order. Driven with the real batch (20) and tick (20 s, INKCAST_SCHEDULER_INTERVAL in staging
and production) and a virtual clock, a Git import of files dated in the past, all due at once, drained at 20 posts
per tick: from 200 files the critical InkcastSloBurnScheduleTimeliness fired (30% of the sends over the SLO's 120 s,
past the 14.4% burn rule), from 900 the critical InkcastPostsOverdue page too, on a scheduler at its full rate. A post
a person had scheduled for 60 s into a 900-file drain went out 840 s late, queued behind the whole import. And with
``created_at`` the API pod's stamp and the lag the scheduler's clock, a skew between them read every due-at-once post
as late.

ADR-0019: a post made after its time (``created_at > run_at``: a Git file dated in the past, a time moved into the
past) is a **backfill**: due at once, sent in order of creation after every post whose time has come, and measured by
no deadline, because it has no time to be on time for. Its send is not in ``inkcast_publish_lag_seconds`` (the SLO)
nor in the customer's on-time rate (``lag_seconds`` None on the event); it stays in the queue's depth and head-of-line
lag. The lag of a **promised** post (made before its time) is the scheduler's clock against the time as written: one
measure, one clock. Items 3 to 5 of the issue are below too. Each ``Finding`` test failed on main before the fix
committed with it."""

import asyncio
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.libs.config import Config, RetryPolicy
from inkcast.libs.content.story import read_when
from inkcast.libs.contracts.models import SyncFile, SyncRequest, SyncResult
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Platform, Prepared, PublishResult
from inkcast.libs.runtime import build_metrics
from inkcast.services.publishing.sync import GitSync

SOURCE = "github.com/acme/blog"
PATH = "posts/dated.md"
BATCH, TICK = 20, 20.0  # PostsRepo.claim_due_posts's limit; Config.scheduler_interval_seconds (staging, production: 20)
DAY = 86400.0


class Clocked(Platform):
    """Answers every send at once with success, and counts them."""

    id, name, default_api_url, credential_fields = "clocked", "Clocked", "https://clocked.invalid", []

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sends = 0

    async def verify(self, creds):
        return Account(id="1", name="clocked")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        return PublishResult(url=f"https://clocked/{self.sends}", remote_id=str(self.sends), status="published")


def markdown(publish_at: datetime | str | None, body: str = "Body.") -> str:
    when = publish_at.isoformat() if isinstance(publish_at, datetime) else publish_at
    return f"---\ntitle: Dated\nplatforms: [clocked]\n{f'publish_at: {when}' + chr(10) if when else ''}---\n\n{body}\n"


def at(seconds_from_now: float) -> datetime:
    """A time that many seconds from now, to the second (as front matter is usually written)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).replace(microsecond=0)


class Followups(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = Clocked()
        self.store.save_credentials(self.user, "clocked", {"k": "v"}, {"id": "1", "name": "on-call"})
        self.publisher = Publisher(self.store, {"clocked": self.platform}, retry=RetryPolicy(4, (60, 300, 1800)))
        config = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:")
        self.publisher.metrics = build_metrics(self.store, {"clocked": self.platform}, config)
        self.git = GitSync(self.store, {"clocked": self.platform}, self.publisher)

    def sync(self, publish_at: datetime | str | None, body: str = "Body.", dry_run: bool = False,
             path: str = PATH) -> SyncResult:
        req = SyncRequest(source=SOURCE, files=[SyncFile(path=path, markdown=markdown(publish_at, body))], dry_run=dry_run)
        (result,) = asyncio.run(self.git.sync(self.user, req)).results
        return result

    def post(self, run_at: float, created_at: float) -> str:
        """A scheduled post as a row: made at ``created_at`` for ``run_at`` (a Git import's or a person's)."""
        prepared = Prepared(platform="clocked", title="T", payload={"body": "Body."}, preview={})
        return self.store.create_post(group_id="g", user_id=self.user, platform="clocked", title="T", status="scheduled",
                                      run_at=run_at, payload=prepared.to_dict(), created_at=created_at)

    def tick(self, now: float) -> int:
        """One scheduler tick at ``now``; how many posts it sent."""
        return len(asyncio.run(self.publisher.run_due(now=now)))

    def lag_count(self) -> int:
        """Sends the schedule-timeliness SLO measured (``inkcast_publish_lag_seconds_count``); 0 when none was."""
        found = re.search(r'inkcast_publish_lag_seconds_count\{platform="clocked"\} (\d+)', self.publisher.metrics.render())
        return int(found.group(1)) if found else 0

    def lag_bucket(self, le: str) -> int:
        found = re.search(rf'inkcast_publish_lag_seconds_bucket\{{platform="clocked",le="{re.escape(le)}"\}} (\d+)',
                          self.publisher.metrics.render())
        return int(found.group(1)) if found else 0

    def published(self) -> list:
        return [e for e in self.store.poll("followups", limit=1000) if e.type == "post.published"]

    def status(self, post_id: str) -> str:
        return self.store.get_post(post_id)["status"]


class FindingABackfillSendIsNotMeasured(Followups):
    def test_an_import_of_past_dated_files_does_not_burn_the_timeliness_slo(self):
        """160 files dated last year, synced at once, drained at the real batch and tick. Measured on main: the eighth
        batch was claimed 140 s after the import, over the SLO's 120 s, so 20 of the 160 sends were late (12.5%; at
        200 files 30%, past the critical burn rule's 14.4%), and ``posts_late`` counted them for the customer. A post
        made after its time has no time to be on time for: its send is measured by no deadline (ADR-0019)."""
        t0 = time.time()
        ids = [self.post(run_at=t0 - 365 * DAY, created_at=t0) for _ in range(8 * BATCH)]
        now = t0
        for _ in range(8):
            self.assertEqual(self.tick(now), BATCH)
            now += TICK
        self.assertEqual({self.status(i) for i in ids}, {"published"}, "every file went out, in eight ticks")
        self.assertEqual(self.lag_count(), 0, "a backfill's send is not in the SLO's histogram")
        events = self.published()
        self.assertEqual(len(events), 8 * BATCH, "each send is a published post for the customer's counts")
        self.assertEqual({(e.attrs["scheduled"], e.attrs["lag_seconds"]) for e in events}, {(True, None)},
                         "scheduled, and neither on time nor late (kpi.rollup counts only a send with a lag)")

    def test_a_backfill_post_still_counts_in_the_queues_depth_and_head_of_line_lag(self):
        """What stays (the issue's constraint: no post class leaves every signal): while it waits, a backfill is in
        ``inkcast_posts_due`` and in ``inkcast_posts_oldest_due_age_seconds``, measured from its creation, so a
        long drain shows as head-of-line lag (InkcastQueueHeadLagHigh, a warning), never as a page."""
        now = time.time()
        self.post(run_at=now - 365 * DAY, created_at=now - 700)
        self.assertEqual(self.store.due_count(now), 1)
        self.assertAlmostEqual(self.store.oldest_due_age(now), 700, delta=1)


class FindingAnImportDoesNotDelayAPromisedPost(Followups):
    def test_a_post_due_during_an_import_is_claimed_in_the_first_batch(self):
        """Measured on main: a post a person scheduled for 60 s into a 900-file import went out 840 s late. The claim
        took the longest wait first, and every file's creation came before the post's time, so the post queued behind
        the whole import. A post whose time has come is claimed before any backfill (ADR-0019): the import, which
        asked for no time, waits for it, not the other way round."""
        t0 = time.time()
        files = [self.post(run_at=t0 - 365 * DAY, created_at=t0) for _ in range(50)]
        promised = self.post(run_at=t0 + 60, created_at=t0 - 3600)
        self.assertEqual(self.tick(t0 + 60), BATCH)
        self.assertEqual(self.status(promised), "published", "due at this tick, yet queued behind 50 files made after it")
        self.assertEqual(sum(self.status(i) == "published" for i in files), BATCH - 1, "the rest of the batch is the import's")
        self.assertEqual((self.lag_count(), self.lag_bucket("60")), (1, 1), "the promise alone is measured: on time")

    def test_backfill_posts_go_out_in_the_order_they_were_made(self):
        """What the order keeps among backfill posts: the one made first goes first (two imports, the older one ahead),
        whatever the dates written in the files."""
        t0 = time.time()
        later_import = [self.post(run_at=t0 - 400 * DAY, created_at=t0 - 10) for _ in range(BATCH)]
        earlier_import = [self.post(run_at=t0 - DAY, created_at=t0 - 20) for _ in range(5)]
        self.tick(t0)
        self.assertEqual({self.status(i) for i in earlier_import}, {"published"}, "made 20 s ago: ahead of the import made 10 s ago")
        self.assertEqual(sum(self.status(i) == "published" for i in later_import), BATCH - 5)


class FindingOverdueCountsPromisesOnly(Followups):
    def test_a_backfill_waiting_past_the_grace_is_not_overdue(self):
        """Measured on main: 900 files dated in the past, draining at 20 per tick, kept ``inkcast_posts_overdue`` above
        0 for over 5 minutes and paged InkcastPostsOverdue (critical) on a scheduler at its full rate. A backfill has
        no time to be overdue against (ADR-0019): the drain is the head-of-line lag's to warn about."""
        now = time.time()
        self.post(run_at=now - 365 * DAY, created_at=now - 700)
        self.assertEqual(self.store.overdue_count(now, 600), 0, "a backfill past the grace: not overdue")
        self.assertEqual(self.store.due_count(now), 1, "due all the same")
        self.assertGreaterEqual(self.store.oldest_due_age(now), 700, "and the head-of-line lag says how long it has waited")

    def test_a_promised_post_the_scheduler_is_behind_on_is_overdue_and_late(self):
        """What the measure keeps (the issue's constraint): a post made before its time, and 700 s past it with the
        scheduler stopped, is overdue (InkcastPostsOverdue pages) and, once claimed, late in the SLO's histogram."""
        now = time.time()
        post = self.post(run_at=now - 700, created_at=now - 7200)
        self.assertEqual(self.store.overdue_count(now, 600), 1)
        self.assertGreaterEqual(self.store.oldest_due_age(now), 700)
        self.tick(now)
        self.assertEqual(self.status(post), "published")
        self.assertEqual((self.lag_count(), self.lag_bucket("120"), self.lag_bucket("+Inf")), (1, 0, 1), "700 s late")
        (event,) = self.published()
        self.assertAlmostEqual(event.attrs["lag_seconds"], 700, delta=1, msg="late for the customer's on-time rate too")


class FindingOneMeasureOneClock(Followups):
    def test_a_creation_stamped_by_a_clock_behind_the_schedulers_makes_no_post_late(self):
        """``created_at`` is stamped by the API pod's clock; the lag is taken on the scheduler's. On main the lag of a
        post made after its time was ``now - created_at``: an API clock 130 s behind the scheduler's made every
        due-at-once post read 130 s late on the tick that sent it, a false burn of the SLO. The measure is now one
        clock, the scheduler's, against the time as written; ``created_at`` only says whether the post was made after
        its time, and a backfill is measured by no deadline (ADR-0019)."""
        now = time.time()
        post = self.post(run_at=now - 365 * DAY, created_at=now - 130)  # made just now, as a clock 130 s behind stamps it
        self.tick(now)
        self.assertEqual(self.status(post), "published")
        self.assertEqual(self.lag_count(), 0, "not 130 s late: not measured")

    def test_a_promised_posts_lag_is_its_time_against_the_schedulers_clock_whatever_the_creation_says(self):
        """A post made before its time is measured from that time alone: the creation's clock cannot move it."""
        now = time.time()
        for skew in (-130.0, 0.0, 130.0):
            with self.subTest(skew=skew):
                self.setUp()
                self.post(run_at=now - 30, created_at=now - 30 - 3600 + skew)
                self.tick(now)
                (event,) = self.published()
                self.assertAlmostEqual(event.attrs["lag_seconds"], 30, delta=1)


class FindingASubSecondMoveIsAChange(Followups):
    def test_a_time_moved_later_by_half_a_second_re_schedules_at_the_new_time(self):
        """On main a change under 1 s was not one (``abs(run_at - wanted) >= 1``): a time moved later by 0.5 s left the
        post due 0.5 s before the time written, against ADR-0015's "nothing is sent before its written time". The
        comparison is exact at the datetime's resolution: a written time's ``timestamp()`` reads back as that time
        (measured over 300,000 random times in 2026-2029: no mismatch, and a microsecond's move always reads as one)."""
        when = at(3600).replace(microsecond=200000)
        created = self.sync(when)
        self.assertEqual((created.action, created.status), ("create", "scheduled"))
        moved = when + timedelta(milliseconds=500)
        result = self.sync(moved)
        self.assertEqual((result.action, result.status), ("update", "scheduled"), "half a second later is a change")
        self.assertNotEqual(result.post_id, created.post_id)
        self.assertEqual(self.store.get_post(result.post_id)["run_at"], moved.timestamp(), "due at the new time, exactly")
        self.assertEqual(self.status(created.post_id), "cancelled")

    def test_a_time_left_alone_is_unchanged_to_the_microsecond(self):
        """What the exact comparison keeps: the same written time, microseconds and all, is no change."""
        when = at(3600).replace(microsecond=123456)
        created = self.sync(when)
        again = self.sync(when)
        self.assertEqual((again.action, again.post_id), ("unchanged", created.post_id))

    def test_a_fraction_beyond_a_microsecond_is_read_at_the_datetimes_resolution(self):
        """Closed with a reason, not changed: ``datetime`` holds microseconds, so a seventh digit is dropped and the
        post is due under a microsecond before the time written, inside the same scheduler tick. Refusing such a time
        would fail files written by tools that print nanoseconds."""
        self.assertEqual(read_when("2026-11-05T09:00:00.1234567+00:00").microsecond, 123456)
        self.assertEqual(read_when("2026-11-05T09:00:00.123456789Z"), read_when("2026-11-05T09:00:00.123456Z"))


class FindingADryRunSaysWhatARemovedTimeDoes(Followups):
    def test_a_dry_run_of_a_removed_time_on_a_scheduled_post_says_it_would_publish_now(self):
        """On main (before #217 too) the dry run of a scheduled post whose file lost its ``publish_at`` said "Would
        re-schedule with the new content", and the real sync cancelled the post and published at once. The dry run
        says what the real run does: a removed time is a "now" post (ADR-0015)."""
        created = self.sync(at(3600))
        dry = self.sync(None, dry_run=True)
        self.assertEqual((dry.action, dry.status, dry.post_id), ("update", "preview", created.post_id))
        self.assertIn("publish", dry.message.lower())
        self.assertIn("now", dry.message.lower())
        self.assertNotIn("re-schedule", dry.message)
        self.assertEqual((self.platform.sends, self.status(created.post_id)), (0, "scheduled"), "a dry run changes nothing")
        real = self.sync(None)
        self.assertEqual((real.action, real.status, self.platform.sends), ("update", "published", 1), "as the dry run said")

    def test_a_dry_run_of_a_changed_time_still_says_it_would_re_schedule(self):
        """What the dry run keeps for a time that is still there."""
        created = self.sync(at(3600))
        dry = self.sync(at(7200), dry_run=True)
        self.assertEqual((dry.action, dry.status, dry.post_id), ("update", "scheduled", created.post_id))
        self.assertIn("re-schedule", dry.message)


class FindingAnInvalidTimeNamesThePostThatStands(Followups):
    def test_a_time_that_fails_validation_fails_the_file_and_names_the_scheduled_post_that_goes_out_unchanged(self):
        """A scheduled post, then a push whose ``publish_at`` is beyond the horizon or has no zone. On main the result
        said only why the file failed; the post from the last valid version stayed scheduled and went out at its
        time, unmentioned. Reporting, not acting, is right: an invalid file changes nothing, as every ``failed``
        result (a cancel would lose a scheduled post to a typo, with CI's red the only notice). The result now names
        the post that stands and when it goes out, so the author can fix the file before then, or cancel it."""
        when = at(3600)
        created = self.sync(when)
        for bad in ((at(731 * DAY)).isoformat(), "2026-11-05T09:00:00"):
            with self.subTest(publish_at=bad):
                failed = self.sync(bad)
                self.assertEqual((failed.action, failed.platform), ("failed", "*"))
                self.assertIn(created.post_id, failed.message, "the post that stands is named")
                self.assertIn(when.isoformat(timespec="seconds"), failed.message, "and when it goes out")
                self.assertIn("cancel", failed.message)
                self.assertEqual((self.status(created.post_id), self.platform.sends), ("scheduled", 0), "unchanged")

    def test_a_file_with_no_post_fails_with_the_reason_alone(self):
        """A new file that fails validation names no post: there is none. YAML reads an unquoted date-time as a
        datetime, and the reason showed its repr (``datetime.datetime(2026, 11, 5, 9, 0)``); it shows it as written."""
        failed = self.sync("2026-11-05T09:00:00")
        self.assertEqual(failed.action, "failed")
        self.assertIn("publish_at 2026-11-05T09:00:00 has no time zone", failed.message)
        self.assertNotIn("datetime.datetime", failed.message)
        self.assertNotIn("goes out", failed.message)


class HeldWhatStaysOpen(Followups):
    def test_a_schedule_only_change_to_a_post_a_person_cancelled_reads_unchanged(self):
        """ADR-0015's open exception, now stated in docs/git-publishing.md: a file whose post a person cancelled is
        published again only once its content changes; a push that moves only its time is ``unchanged``."""
        created = self.sync(at(3600))
        self.publisher.cancel(self.user, created.post_id)
        self.assertEqual(self.sync(at(7200)).action, "unchanged", "the time alone moved: ignored (open)")
        edited = self.sync(at(7200), body="New body.")
        self.assertEqual((edited.action, edited.status), ("create", "scheduled"), "the content changed: a new post")
        self.assertNotEqual(edited.post_id, created.post_id)


if __name__ == "__main__":
    unittest.main()
