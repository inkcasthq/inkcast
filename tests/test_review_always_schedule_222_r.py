# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #222 (PR #439, ADR-0019): a backfill is a post made after the time asked for, and stays one through the
scheduler's own moves of ``run_at``.

ADR-0019 classes a post by ``created_at > run_at``. ``run_at`` is the time asked for only until the scheduler moves it:
a retry (a 429's ``Retry-After``, a connection failure: ``Publisher._deliver``) and recovery's re-send
(``Publisher.recover``) set it to the retry's time, which is after the creation. A backfill the platform made wait once
was then a promise: claimed ahead of the person's posts, counted overdue, measured in the SLO's histogram and in the
customer's ``posts_on_time``, against ADR-0019 and docs/git-publishing.md ("in neither posts_on_time nor posts_late").
Driven on bf565bb with the real ``run_due`` on a virtual clock, a 900-file import to a platform down for 30 minutes
counted 629 of its 900 sends in the customer's on-time rate, and a person's posts on a healthy platform waited up to two
ticks behind the import's retries. The time asked for is ``requested_at`` (``PostsRepo`` keeps it through every retry
and recovery, ADR-0017); a post asked for now, or a row from before the column, has none, and its ``run_at`` stands.
Each test failed on bf565bb."""

import asyncio
import re
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.config import Config, RetryPolicy
from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.libs.runtime import build_metrics

DAY = 86400.0


class Waits(Platform):
    """Rate-limits the first ``refuse`` sends (a 429 with Retry-After: 60), then publishes; can say a post is missing."""

    id, name, default_api_url, credential_fields = "waits", "Waits", "https://waits.invalid", []
    capabilities = frozenset({"find"})

    def __init__(self, refuse: int = 0):
        super().__init__(httpx2.AsyncClient())
        self.refuse, self.sends = refuse, 0

    async def verify(self, creds):
        return Account(id="1", name="waits")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        if self.refuse:
            self.refuse -= 1
            raise PlatformError("Rate-limited.", retryable=True, retry_after=60)
        self.sends += 1
        return PublishResult(url=f"https://waits/{self.sends}", remote_id=str(self.sends), status="published")

    async def find_existing(self, creds, account, prepared):
        return None  # the interrupted send never arrived


class Backfill(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = Waits()
        self.store.save_credentials(self.user, "waits", {"k": "v"}, {"id": "1", "name": "on-call"})
        self.publisher = Publisher(self.store, {"waits": self.platform}, retry=RetryPolicy(4, (60, 300, 1800)))
        config = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:")
        self.publisher.metrics = build_metrics(self.store, {"waits": self.platform}, config)

    def post(self, run_at: float, created_at: float) -> str:
        prepared = Prepared(platform="waits", title="T", payload={"body": "Body."}, preview={})
        return self.store.create_post(group_id="g", user_id=self.user, platform="waits", title="T", status="scheduled",
                                      run_at=run_at, payload=prepared.to_dict(), created_at=created_at)

    def tick(self, now: float | None = None) -> None:
        asyncio.run(self.publisher.run_due(now=now))

    def lag_count(self) -> int:
        found = re.search(r'inkcast_publish_lag_seconds_count\{platform="waits"\} (\d+)', self.publisher.metrics.render())
        return int(found.group(1)) if found else 0

    def published(self) -> list:
        return [e for e in self.store.poll("review-222", limit=1000) if e.type == "post.published"]


class FindingARetriedBackfillStaysABackfill(Backfill):
    def test_a_backfill_the_platform_made_wait_is_never_overdue_nor_measured_when_it_goes_out(self):
        """A file dated a day ago, synced now: a backfill. Its first send is rate-limited, so the scheduler re-queues it
        at the retry's time, after its creation. On bf565bb that made it a promise: overdue 700 s after the retry
        (InkcastPostsOverdue pages), and its send measured (the SLO's histogram, the customer's ``posts_on_time``)."""
        t0 = time.time()
        self.platform.refuse = 1
        post = self.post(run_at=t0 - DAY, created_at=t0)
        self.tick(t0)
        row = self.store.get_post(post)
        self.assertEqual((row["status"], row["attempts"]), ("scheduled", 1), "waits for its retry")
        self.assertEqual(self.store.overdue_count(row["run_at"] + 700, 600), 0, "a backfill is never overdue (ADR-0019)")
        self.tick(row["run_at"] + 1)
        self.assertEqual(self.store.get_post(post)["status"], "published")
        self.assertEqual(self.lag_count(), 0, "a backfill's send is not in the SLO's histogram")
        (event,) = self.published()
        self.assertIsNone(event.attrs["lag_seconds"], "neither on time nor late for the customer")

    def test_a_retried_backfill_does_not_go_out_ahead_of_a_promise(self):
        """Twenty files dated a day ago, each rate-limited once, and a person's post due 5 s after their retries. On
        bf565bb the retried files were promises due before it, and took the whole batch: the person's post waited a
        tick behind the import, which ADR-0019's order exists to prevent."""
        t0 = time.time()
        self.platform.refuse = 20
        files = [self.post(run_at=t0 - DAY, created_at=t0) for _ in range(20)]
        self.tick(t0)
        retry_at = max(self.store.get_post(f)["run_at"] for f in files)
        promised = self.post(run_at=retry_at + 5, created_at=t0 - 3600)
        self.tick(retry_at + 5)
        self.assertEqual(self.store.get_post(promised)["status"], "published", "queued behind the import's retries")
        self.assertEqual(sum(self.store.get_post(f)["status"] == "published" for f in files), 19)

    def test_a_backfill_recovery_sends_again_is_still_a_backfill(self):
        """A backfill whose send was interrupted (a crash mid-send), which recovery finds missing and queues again at
        that moment: on bf565bb a promise from then on, measured, and counted on time for the customer."""
        t0 = time.time()
        post = self.post(run_at=t0 - DAY, created_at=t0 - 1)
        self.assertEqual([r["id"] for r in self.store.claim_due_posts(t0)], [post])  # claimed; the process then stops
        self.store._exec("UPDATE posts SET updated_at = ? WHERE id = ?", t0 - STUCK_AFTER - 1, post)
        self.tick()  # recovery: missing, so queued again
        self.assertEqual(self.store.get_post(post)["status"], "scheduled")
        self.tick(time.time() + 1)
        self.assertEqual(self.store.get_post(post)["status"], "published")
        self.assertEqual(self.lag_count(), 0)
        (event,) = self.published()
        self.assertIsNone(event.attrs["lag_seconds"])

    def test_a_promise_the_platform_made_wait_is_still_measured_from_its_retry(self):
        """What the fix keeps: a post made before its time, rate-limited once, is a promise: overdue past the grace
        after its retry's time, and its send measured from that time (the scheduler's clock against ``run_at``)."""
        t0 = time.time()
        self.platform.refuse = 1
        post = self.post(run_at=t0 - 5, created_at=t0 - 3600)
        self.tick(t0)
        retry_at = self.store.get_post(post)["run_at"]
        self.assertEqual(self.store.overdue_count(retry_at + 700, 600), 1)
        self.tick(retry_at + 30)
        self.assertEqual(self.lag_count(), 2, "both claims measured")
        (event,) = self.published()
        self.assertAlmostEqual(event.attrs["lag_seconds"], 30, delta=1)

    def test_a_now_post_the_platform_made_wait_is_measured_as_before(self):
        """What the fix keeps: a post asked for now (a Git file with no publish_at: no ``requested_at``) that the
        platform rate-limits waits for the scheduler, which measures its send from the retry's time, as before #222."""
        t0 = time.time()
        prepared = Prepared(platform="waits", title="T", payload={"body": "Body."}, preview={})
        post = self.store.create_post(group_id="g", user_id=self.user, platform="waits", title="T", status="publishing",
                                      run_at=t0 - 1, payload=prepared.to_dict())
        self.store.update_post(post, status="scheduled", run_at=t0 + 60, not_before=t0 + 60, attempts=1)
        self.tick(t0 + 90)
        (event,) = self.published()
        self.assertAlmostEqual(event.attrs["lag_seconds"], 30, delta=1)


if __name__ == "__main__":
    unittest.main()
