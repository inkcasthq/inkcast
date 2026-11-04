# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #411 (issue #141): outbox housekeeping.

``sync_consumers`` retired every cursor whose name the running release's ``CONSUMERS`` lacks, at once. Across releases
that name can belong to a consumer that is live: during a rollout or a rollback the scheduler of one release runs beside
the consumer pods of another (each Deployment rolls on its own). The older scheduler then deleted the newer release's
live cursor, purged the events that consumer had not handled yet, and its next poll started again from seq 0. A cursor
is retired only once no poller has touched it for ``RETIRE_IDLE``: a poller refreshes its row on every pass, even an
empty one (``OutboxPoller.run_once``).
"""

from __future__ import annotations

import asyncio
import time
import types
import unittest

from fakes import seed_people

from inkcast.libs.db import Store
from inkcast.libs.events import CONSUMERS, DomainEvent
from inkcast.libs.outbox import OutboxPoller
from inkcast.libs.config import RetentionPolicy
from inkcast.services import scheduler
from inkcast.services.scheduler import housekeeping, run_scheduler

DAY = 86400.0
KEY = "k" * 40


def published(at: float) -> DomainEvent:
    return DomainEvent("post.published", user_id="u1", subject="p", at=at,
                       attrs={"platform": "devto", "status": "published", "scheduled": False, "lag_seconds": None})


def context(store: Store) -> types.SimpleNamespace:
    return types.SimpleNamespace(store=store, config=types.SimpleNamespace(retention=RetentionPolicy()))


class AConsumerAnotherReleaseRunsIsNotRetiredWhileItPolls(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", KEY)
        self.addCleanup(self.store.close)
        seed_people(self.store, "u1")
        self.assertEqual(list(CONSUMERS), ["insights"], "this release's scheduler knows only insights")

    def seqs(self, consumer: str) -> list[int]:
        return [e.seq for e in self.store.poll(consumer)]

    def test_an_older_scheduler_neither_purges_nor_resets_a_newer_releases_live_consumer(self):
        """The next release adds ``webhooks``; its pod is up and has handled seq 1 of 3 (8 days old) while this
        release's scheduler still runs. On 835a7c3 the cursor goes, the purge takes seq 2 and 3, and the consumer's
        next poll finds nothing: two events a live consumer had not handled, lost."""
        self.store.enqueue(*(published(time.time() - 8 * DAY) for _ in range(3)))
        self.store.advance("insights", 3)
        self.store.advance("webhooks", 1)                       # polled just now, by a pod of the newer release
        housekeeping(context(self.store))
        self.assertEqual(self.store.cursor("webhooks"), 1, "its position kept")
        self.assertEqual(self.seqs("webhooks"), [2, 3], "and the events it has not handled")

    def test_a_pod_of_the_old_release_still_polling_a_retired_name_does_not_replay_from_the_start(self):
        """The PR's own rollout case: the old pod's cursor went every housekeeping, so its next poll replayed the whole
        outbox from seq 0 (once per housekeeping, for as long as the pod ran)."""
        self.store.enqueue(*(published(time.time()) for _ in range(3)))
        self.store.advance("insights", 3)
        OutboxPoller(self.store, "legacy", []).drain()          # the old pod, caught up and polling
        housekeeping(context(self.store))
        self.assertEqual(self.seqs("legacy"), [], "nothing handled twice")

    def test_the_scheduler_start_leaves_a_live_cursor_of_another_release_alone(self):
        self.store.enqueue(*(published(time.time() - 8 * DAY) for _ in range(3)))
        self.store.advance("insights", 3)
        self.store.advance("webhooks", 1)

        class Publisher:
            async def run_forever(self, interval, stop, housekeeping=None, on_tick=None):
                return None

        ctx = context(self.store)
        ctx.publisher, ctx.config.scheduler_interval_seconds = Publisher(), 0.001
        asyncio.run(run_scheduler(ctx, asyncio.Event()))
        self.assertEqual(self.store.cursor("webhooks"), 1)

    def test_a_cursor_nobody_has_polled_for_the_idle_period_is_still_retired(self):
        """What the issue asks stays true: a retired consumer no longer holds the purge."""
        self.store.enqueue(*(published(time.time() - 8 * DAY) for _ in range(3)))
        self.store.advance("insights", 3)
        self.store.advance("legacy", 1)
        self.store._exec("UPDATE outbox_cursors SET at = ? WHERE consumer = 'legacy'",
                         time.time() - scheduler.RETIRE_IDLE)
        with self.assertLogs("inkcast.services.scheduler", level="WARNING") as logs:
            housekeeping(context(self.store))
        self.assertEqual([getattr(r, "consumer", "") for r in logs.records], ["legacy"])
        self.assertNotIn("legacy", self.store.outbox_lag())
        self.assertEqual(self.seqs("insights"), [], "purged: held by nobody live")


if __name__ == "__main__":
    unittest.main()
