# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #174: a ``PlatformError`` subclass that skips the base constructor must not crash the scheduler tick.

A third-party plug-in written against the documented contract (``retryable``, ``uncertain``) can raise a subclass
that sets those itself and never calls ``PlatformError.__init__``: it has no ``retry_after``, the field #168 added.
``_send`` read ``exc.retry_after`` directly, so the ``AttributeError`` escaped ``run_due``: the tick died, the post
stayed 'publishing', and so did every post claimed after it in the batch, until recovery took them over ten minutes
later (and on a platform that can't be checked, marked them unconfirmed although nothing was ever sent).

Each test failed on main before the fix committed with it."""

import asyncio
import time
import unittest

import httpx2

from inkcast.libs.config import RetryPolicy
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult


class PreRetryAfterError(PlatformError):
    """A plug-in's error from before #168: the documented flags, set by hand, and no call to the base constructor."""

    def __init__(self, message: str, retryable: bool = False, uncertain: bool = False):
        Exception.__init__(self, message)
        self.retryable = retryable
        self.uncertain = uncertain


class BareError(PlatformError):
    """A subclass that sets nothing at all: no flag the pipeline reads exists on it."""

    def __init__(self, message: str):
        Exception.__init__(self, message)


class Odd(Platform):
    """Raises the error it is given on every publish, and counts the sends."""

    id, name, default_api_url, credential_fields = "odd", "Odd", "https://odd.invalid", []

    def __init__(self, error: BaseException):
        super().__init__(httpx2.AsyncClient())
        self.error = error
        self.sends = 0

    async def verify(self, creds):
        return Account(id="1", name="odd")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        raise self.error


class Fine(Platform):
    """A well-behaved platform sharing the batch: every publish succeeds."""

    id, name, default_api_url, credential_fields = "fine", "Fine", "https://fine.invalid", []

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sends = 0

    async def verify(self, creds):
        return Account(id="1", name="fine")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        return PublishResult(url=f"https://fine/{self.sends}", remote_id=str(self.sends), status="published")


class OneBatch(unittest.TestCase):
    """Two due posts claimed in one tick: the odd plug-in's first, then a well-behaved platform's."""

    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.fine = Fine()

    def batch(self, odd: Odd) -> tuple[Publisher, str, str]:
        for platform in (odd, self.fine):
            self.store.save_credentials(self.user, platform.id, {"k": "v"}, {"id": "1", "name": platform.id})
        publisher = Publisher(self.store, {odd.id: odd, self.fine.id: self.fine}, retry=RetryPolicy(4, (60, 300, 1800)))
        posts = []
        for platform, run_at in ((odd, time.time() - 2), (self.fine, time.time() - 1)):   # the odd one is claimed first
            prepared = platform.prepare({"title": "t"}, "Body.")
            posts.append(self.store.create_post(group_id="g", user_id=self.user, platform=platform.id, title="t",
                                                status="scheduled", run_at=run_at, payload=prepared.to_dict()))
        return publisher, posts[0], posts[1]

    def test_an_error_without_retry_after_is_retried_under_the_policy_and_the_batch_goes_on(self):
        odd = Odd(PreRetryAfterError("Odd rate limit hit; try again shortly.", retryable=True))
        publisher, odd_post, fine_post = self.batch(odd)
        asyncio.run(publisher.run_due())
        post = self.store.get_post(odd_post)
        self.assertEqual((post["status"], post["attempts"], odd.sends), ("scheduled", 1, 1))
        self.assertAlmostEqual(post["run_at"] - post["updated_at"], 60, delta=2, msg="the policy's own delay")
        self.assertEqual((self.store.get_post(fine_post)["status"], self.fine.sends), ("published", 1))
        # The retry happens: the odd error is an ordinary retryable one.
        asyncio.run(publisher.run_due(now=time.time() + 61))
        self.assertEqual((self.store.get_post(odd_post)["attempts"], odd.sends), (2, 2))

    def test_an_error_with_no_flags_at_all_strands_only_its_own_post_as_unconfirmed(self):
        odd = Odd(BareError("something went wrong"))
        publisher, odd_post, fine_post = self.batch(odd)
        with self.assertLogs("inkcast.libs.pipeline", "ERROR"):
            asyncio.run(publisher.run_due())
        post = self.store.get_post(odd_post)
        self.assertEqual((post["status"], odd.sends), ("unconfirmed", 1))
        self.assertIn("internal error", post["error"])
        self.assertEqual((self.store.get_post(fine_post)["status"], self.fine.sends), ("published", 1))
        # Never retried: the pipeline can't tell what the error meant, so the post may be out.
        asyncio.run(publisher.run_due(now=time.time() + 3600))
        self.assertEqual((self.store.get_post(odd_post)["status"], odd.sends), ("unconfirmed", 1))

    def test_a_now_publish_with_such_an_error_fails_cleanly(self):
        """The same error on a "now" post (no retry path): it is reported failed, as a retryable error always was."""
        from inkcast.libs.content.story import parse_story
        odd = Odd(PreRetryAfterError("Odd rate limit hit; try again shortly.", retryable=True))
        publisher, _, _ = self.batch(odd)
        story = parse_story("# Now\n\nBody.\n", known_platforms=["odd"])
        (outcome,) = asyncio.run(publisher.publish(self.user, story, ["odd"]))
        self.assertEqual((outcome.status, odd.sends), ("failed", 1))
        self.assertEqual(self.store.get_post(outcome.post_id)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
