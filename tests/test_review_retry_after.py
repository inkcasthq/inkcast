# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #168 (issue #126): a 429's ``Retry-After`` sets the retry delay, capped by the policy.

Each test failed on the PR head before the fix committed with it."""

import asyncio
import pathlib
import time
import unittest

import httpx2

from inkcast.libs.config import Config, RetryPolicy, load_config
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.libs.secrets import Redactor, run_checks

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
        "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
HOUR = 3600


def rate_limited(retry_after: str):
    """A DEV API that answers every article POST with a 429 carrying this ``Retry-After`` (as bytes off the wire), and
    counts the sends."""
    sends: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and request.url.path == "/api/articles":
            sends.append(request)
            return httpx2.Response(429, headers=[(b"Retry-After", retry_after.encode("utf-8"))],
                                   json={"error": "Rate limit reached"})
        return httpx2.Response(404, json={"error": "not found"})

    return DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(handler))), sends


class Scheduler(unittest.TestCase):
    """A scheduled DEV post, ticked whenever it is due."""

    def publisher(self, platform: DevtoPlatform, policy: RetryPolicy) -> Publisher:
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = platform
        self.store.save_credentials(self.user, platform.id, {"api_key": "k"}, {"id": "1", "name": "on-call"})
        return Publisher(self.store, {platform.id: platform}, retry=policy)

    def due(self) -> str:
        prepared = self.platform.prepare({"title": "t"}, "Body.")
        return self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title="t",
                                      status="scheduled", run_at=time.time() - 1, payload=prepared.to_dict())

    def drive(self, publisher: Publisher, post_id: str) -> tuple[str, list[int]]:
        """Tick whenever the post is due: (the final status, the wait before each retry)."""
        waits: list[int] = []
        for _ in range(40):
            post = self.store.get_post(post_id)
            if post["status"] != "scheduled":
                break
            asyncio.run(publisher.run_due(now=post["run_at"] + 1))
            post = self.store.get_post(post_id)
            if post["status"] == "scheduled":
                waits.append(round(post["run_at"] - post["updated_at"]))
        return self.store.get_post(post_id)["status"], waits


def retry_window_check(cfg: Config):
    return {c.id: c for c in run_checks(cfg, redactor=Redactor(), environ={})}["publish-retry-window"]


class ThePostureCheckCountsRetryAfter(Scheduler):
    def test_a_policy_the_check_passes_holds_a_rate_limited_post_for_42_hours(self):
        """The publish-retry-window posture check warns when a post could still be retried a day after its time
        ("could go out that long after its time with nobody told"). It sums the policy's own delays, but a 429's
        Retry-After now stretches every wait up to the largest delay. Eight sends, 60,300,1800,3600,21600: the check
        passes ("marked failed after 19 h 36 min"), while a platform whose 429s ask for six hours holds the post 42 h
        and sends it on the second day."""
        cfg = load_config({**BASE, "INKCAST_PUBLISH_MAX_ATTEMPTS": "8",
                           "INKCAST_PUBLISH_RETRY_DELAYS": "60,300,1800,3600,21600"})
        policy = cfg.publish_retry
        self.assertEqual(policy, RetryPolicy(8, (60, 300, 1800, 3600, 21600)), "valid settings")
        devto, sends = rate_limited(str(6 * HOUR))
        status, waits = self.drive(self.publisher(devto, policy), self.due())
        self.assertEqual((status, waits, len(sends)), ("failed", [6 * HOUR] * 7, 8))
        self.assertEqual(sum(waits), 42 * HOUR)
        check = retry_window_check(cfg)
        self.assertEqual(check.status, "warn", check.detail)
        self.assertIn("42 h", check.detail)
        self.assertIn("Retry-After", check.detail)
        # The default policy (4 sends, 60,300,1800: at most 1 h 30 min with Retry-After) still passes.
        self.assertEqual(retry_window_check(load_config(BASE)).status, "pass")

    def test_the_posture_table_says_retry_after_counts(self):
        text = (ROOT / "docs/reference/secrets.md").read_text(encoding="utf-8")
        (row,) = [line for line in text.splitlines() if line.startswith("| `publish-retry-window`")]
        self.assertIn("Retry-After", row)


if __name__ == "__main__":
    unittest.main()
