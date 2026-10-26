# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #208 (issue #196): rows written for a person in the instant of their erasure."""

from __future__ import annotations

import time
import unittest

from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.runtime import build_metrics

KEY = "k" * 40
PAYLOAD = {"platform": "fake", "title": "T", "payload": {}, "preview": {}}


def gauge(store: Store, name: str) -> float:
    """One unlabelled gauge as /metrics serves it: what the alert rules read."""
    config = Config(public_url="http://127.0.0.1:1", secret_key="k" * 48, database_path=":memory:")
    lines = [line for line in build_metrics(store, {}, config).render().splitlines() if line.startswith(name + " ")]
    (line,) = lines
    return float(line.split()[1])


class TheQueueGauges(unittest.TestCase):
    """A post whose person has no ``users`` row is never claimed now (``_OWNED``), so it is not in the queue: on
    ``main`` the scheduler claimed a leftover like this at its next tick and it left the queue as failed; on the PR it
    stays 'scheduled' and due until the first housekeeping (90 ticks: 30 minutes at the default 20 s), and the queue
    gauges kept counting it, so ``InkcastPostsOverdue`` (critical) and ``InkcastQueueHeadLagHigh`` fired on a healthy
    scheduler after the upgrade, on exactly the databases the issue is about."""

    def setUp(self):
        self.store = Store(":memory:", KEY)
        self.addCleanup(self.store.close)

    def post(self, user_id: str, run_at: float) -> str:
        return self.store.create_post(group_id="g", user_id=user_id, platform="fake", title="T", status="scheduled",
                                      run_at=run_at, payload=PAYLOAD)

    def test_a_post_no_claim_will_take_is_neither_due_nor_overdue(self):
        now = time.time()
        ada = self.store.upsert_user("oidc:x#ada", "ada", None, None)["id"]
        bob = self.store.upsert_user("oidc:x#bob", "bob", None, None)["id"]
        self.post(ada, now - 3600)                  # an hour past due: past INKCAST_OVERDUE_GRACE (600 s)
        with self.store._lock:                      # the leftover of the race in a database from before the guard
            self.store._conn.execute("DELETE FROM users WHERE id = ?", (ada,))
        self.assertEqual(self.store.claim_due_posts(now), [], "never the scheduler's")
        self.assertEqual(self.store.due_count(now), 0, "not in the queue the scheduler drains")
        self.assertEqual(self.store.overdue_count(now, 600), 0)
        self.assertEqual(self.store.oldest_due_age(now), 0.0)
        self.assertEqual(gauge(self.store, "inkcast_posts_overdue"), 0, "InkcastPostsOverdue: max(...) > 0, critical")
        self.assertEqual(gauge(self.store, "inkcast_posts_oldest_due_age_seconds"), 0, "InkcastQueueHeadLagHigh")
        # A living person's due post counts as it did, and is the one claimed.
        self.post(bob, now - 1200)
        self.assertEqual((self.store.due_count(now), self.store.overdue_count(now, 600)), (1, 1))
        self.assertAlmostEqual(self.store.oldest_due_age(now), 1200, delta=1)
        self.assertEqual([p["user_id"] for p in self.store.claim_due_posts(now)], [bob])
        self.assertEqual((self.store.due_count(now), self.store.oldest_due_age(now)), (0, 0.0))


if __name__ == "__main__":
    unittest.main()
