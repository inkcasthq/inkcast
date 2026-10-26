# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Deferred review of #168 (issue #126): a 429's ``Retry-After`` sets the retry delay, capped by the policy.

Each test in a ``Finding`` class failed on main at 57f83d1 (#168 and its adversarial review merged) before the fix
committed with it."""

import asyncio
import time
import unittest

import httpx2

from inkcast.libs.config import RetryPolicy, load_config
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.libs.secrets import Redactor, run_checks

BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
        "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
HOUR = 3600
DAY = 24 * HOUR


def rate_limited(retry_after: str, paths: tuple[str, ...] = ("/api/articles",)):
    """A DEV API that answers each request whose path is in ``paths`` with a 429 carrying this ``Retry-After`` (bytes
    off the wire), and anything else with success. Records every request."""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path in paths:
            return httpx2.Response(429, headers=[(b"Retry-After", retry_after.encode("utf-8"))],
                                   json={"error": "Rate limit reached"})
        if request.url.path.startswith("/api/organizations/"):
            return httpx2.Response(200, json={"id": 7, "name": "On-call", "username": "on-call"})
        return httpx2.Response(201, json={"id": 1, "url": "https://dev.to/on-call/t", "published": True})

    return DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(handler))), requests


class Scheduler(unittest.TestCase):
    """A scheduled DEV post, ticked whenever it is due."""

    def publisher(self, platform: DevtoPlatform, policy: RetryPolicy | None = None) -> Publisher:
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = platform
        self.store.save_credentials(self.user, platform.id, {"api_key": "k"}, {"id": "1", "name": "on-call"})
        if policy is None:
            return Publisher(self.store, {platform.id: platform})
        return Publisher(self.store, {platform.id: platform}, retry=policy)

    def due(self, **fields) -> str:
        prepared = self.platform.prepare({"title": "t", **fields}, "Body.")
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


def retry_window_check(attempts: str, delays: str):
    cfg = load_config({**BASE, "INKCAST_PUBLISH_MAX_ATTEMPTS": attempts, "INKCAST_PUBLISH_RETRY_DELAYS": delays})
    return {c.id: c for c in run_checks(cfg, redactor=Redactor(), environ={})}["publish-retry-window"], cfg.publish_retry


class FindingThePostureCheckQuotesOnlyTheShortestWindow(Scheduler):
    """``publish-retry-window`` tells the operator when a post that keeps failing is marked failed. After #168 that is
    a range, from the policy's own delays up to every wait stretched to the largest delay by a 429's Retry-After, but
    the check quotes the bottom of the range as the figure, except in the one branch #172 added (stretched over a day,
    policy-only within it). ``INKCAST_PUBLISH_MAX_ATTEMPTS=2, INKCAST_PUBLISH_RETRY_DELAYS=30,21600`` passes with
    "marked failed after 30 s", and the scheduler holds the post for six hours."""

    def test_the_pass_detail_says_how_long_retry_after_can_stretch_the_window(self):
        check, policy = retry_window_check("2", "30,21600")
        self.assertEqual((policy.window_seconds, policy.longest_window_seconds), (30, 6 * HOUR), "valid settings")
        devto, requests = rate_limited(str(6 * HOUR))
        self.assertEqual(self.drive(self.publisher(devto, policy), self.due()), ("failed", [6 * HOUR]))
        self.assertEqual(check.status, "pass", check.detail)
        self.assertIn("30 s", check.detail)
        self.assertIn("6 h", check.detail, "the operator is told 30 s; the post waited six hours")
        self.assertIn("Retry-After", check.detail)
        # The default policy: 36 min by its own delays, up to 1 h 30 min with Retry-After.
        check, policy = retry_window_check("4", "60,300,1800")
        self.assertEqual(policy.longest_window_seconds, 90 * 60)
        self.assertEqual(check.status, "pass", check.detail)
        self.assertIn("36 min", check.detail)
        self.assertIn("1 h 30 min", check.detail)

    def test_the_over_a_day_warning_says_the_stretched_figure_too(self):
        check, policy = retry_window_check("7", "3600,21600")
        self.assertEqual((policy.window_seconds, policy.longest_window_seconds), (31 * HOUR, 36 * HOUR))
        self.assertEqual(check.status, "warn", check.detail)
        self.assertIn("31 h", check.detail)
        self.assertIn("36 h", check.detail, "warned of 31 h; a rate-limited post can go out 36 h after its time")

    def test_a_window_retry_after_cannot_stretch_is_quoted_once(self):
        """One delay, or the largest repeated: there is nothing to stretch, and the figure stands alone."""
        for attempts, delays, said in (("5", "21600", "24 h"), ("10", "30", "4 min 30 s"), ("1", "30", "no retry")):
            with self.subTest(attempts=attempts, delays=delays):
                check, policy = retry_window_check(attempts, delays)
                self.assertEqual(policy.longest_window_seconds, policy.window_seconds)
                self.assertEqual(check.status, "pass", check.detail)
                self.assertIn(said, check.detail)
                self.assertNotIn("Retry-After", check.detail)
        check, policy = retry_window_check("6", "21600")
        self.assertEqual(policy.longest_window_seconds, policy.window_seconds)
        self.assertEqual(check.status, "warn", check.detail)
        self.assertIn("30 h", check.detail)
        self.assertNotIn("Retry-After", check.detail)


if __name__ == "__main__":
    unittest.main()
