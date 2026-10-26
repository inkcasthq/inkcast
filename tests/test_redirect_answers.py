# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #173: a 3xx answer to a publish is a definite failure, never success.

Inkcast's HTTP client doesn't follow redirects, so a 3xx comes back as the platform's answer. ``Platform.request``
checked 429, 5xx and 4xx and treated anything else as success: a 301 with an empty body on DEV's ``POST /articles``
marked the post 'published' with no URL, and one with an HTML body crashed the plug-in reading it, so the post ended
'unconfirmed'. A redirect means the request never reached the API we meant (a wrong API URL, say): nothing was sent,
nothing will change on a retry. So it is a ``PlatformError`` that is neither retryable nor uncertain, on every plug-in.

Each test failed on main before the fix committed with it."""

import asyncio
import time
import unittest

import httpx2

from inkcast.libs.content.story import parse_story
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.libs.platforms.medium import MediumPlatform

REDIRECTS = (301, 302, 307, 308)  # a 303 on a publish is uncertain: tests/test_review_redirect_answers.py
BODIES = {"empty": b"", "html": b"<html><body>Moved</body></html>", "json": b'{"id": 1, "url": "https://x/1"}'}


def redirecting(status: int, body: bytes, location: str | None = "https://elsewhere.invalid/api/articles"):
    """Platforms whose API answers every request with this redirect, and a log of the requests."""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        headers = {"Location": location} if location else {}
        return httpx2.Response(status, headers=headers, content=body)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return [DevtoPlatform(http), MediumPlatform(http)], requests


class EveryPlugin(unittest.TestCase):
    """``Platform.request`` is the one place a reply becomes a result, so both built-in plug-ins are covered."""

    def send(self, platform: Platform) -> PlatformError:
        creds = {"api_key": "k", "token": "t"}
        prepared = platform.prepare({"title": "t"}, "Body.")
        with self.assertRaises(PlatformError) as caught:
            asyncio.run(platform.publish(creds, Account(id="1", name="n"), prepared, None))
        return caught.exception

    def test_a_redirect_on_a_publish_is_a_definite_non_retryable_error(self):
        for status in REDIRECTS:
            for shape, body in BODIES.items():
                platforms, requests = redirecting(status, body)
                for platform in platforms:
                    with self.subTest(platform=platform.id, status=status, body=shape):
                        exc = self.send(platform)
                        self.assertEqual((exc.retryable, exc.uncertain), (False, False))
                        self.assertIsNone(exc.retry_after)
                        self.assertIn(str(status), str(exc))
                        self.assertIn("redirect", str(exc).lower())
                        self.assertIn(platform.name, str(exc))
                self.assertEqual(len(requests), 2, "one request per platform: the redirect was not followed")

    def test_a_redirect_without_a_location_is_the_same_error(self):
        platforms, _ = redirecting(302, b"", location=None)
        for platform in platforms:
            with self.subTest(platform=platform.id):
                exc = self.send(platform)
                self.assertEqual((exc.retryable, exc.uncertain), (False, False))

    def test_a_redirect_on_a_lookup_is_a_definite_error_too(self):
        """verify (GET) runs through the same helper: a redirected API is a wrong API URL, not a bad credential."""
        platforms, _ = redirecting(301, b"")
        for platform in platforms:
            with self.subTest(platform=platform.id), self.assertRaises(PlatformError) as caught:
                asyncio.run(platform.verify({"api_key": "k", "token": "t"}))
            self.assertEqual((caught.exception.retryable, caught.exception.uncertain), (False, False))


class ThroughThePipeline(unittest.TestCase):
    """No post is marked 'published' without a confirmed platform answer, and nothing is retried."""

    def publisher(self, platform: Platform) -> Publisher:
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.store.save_credentials(self.user, platform.id, {"api_key": "k", "token": "t"},
                                    {"id": "1", "name": "on-call"})
        return Publisher(self.store, {platform.id: platform})

    def test_a_now_publish_fails_with_no_url(self):
        for status in REDIRECTS:
            for shape, body in BODIES.items():
                platforms, requests = redirecting(status, body)
                for platform in platforms:
                    with self.subTest(platform=platform.id, status=status, body=shape):
                        publisher = self.publisher(platform)
                        story = parse_story("# Now\n\nBody.\n", known_platforms=[platform.id])
                        (outcome,) = asyncio.run(publisher.publish(self.user, story, [platform.id]))
                        post = self.store.get_post(outcome.post_id)
                        self.assertEqual((outcome.status, post["status"]), ("failed", "failed"))
                        self.assertIsNone(outcome.url)
                        self.assertIsNone(post["result_url"])
                        self.assertIn("redirect", post["error"].lower())

    def test_a_scheduled_post_fails_once_and_is_never_retried(self):
        for shape, body in BODIES.items():
            platforms, requests = redirecting(301, body)
            for platform in platforms:
                with self.subTest(platform=platform.id, body=shape):
                    publisher = self.publisher(platform)
                    prepared = platform.prepare({"title": "t"}, "Body.")
                    post_id = self.store.create_post(group_id="g", user_id=self.user, platform=platform.id, title="t",
                                                     status="scheduled", run_at=time.time() - 1,
                                                     payload=prepared.to_dict())
                    sent = len(requests)
                    asyncio.run(publisher.run_due())
                    post = self.store.get_post(post_id)
                    self.assertEqual((post["status"], post["attempts"], len(requests) - sent), ("failed", 1, 1))
                    self.assertIsNone(post["result_url"])
                    for later in (61, 301, 1801, 7 * 24 * 3600):
                        asyncio.run(publisher.run_due(now=time.time() + later))
                    self.assertEqual((self.store.get_post(post_id)["status"], len(requests) - sent), ("failed", 1))


if __name__ == "__main__":
    unittest.main()
