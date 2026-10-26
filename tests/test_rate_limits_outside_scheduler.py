# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #188, items 2 to 5: rate limits outside the scheduler loop.

2. A Git sync "now" post that gets a 429 is handed to the scheduler, which retries it under the policy (and the
   429's Retry-After), instead of failing at once and being created afresh by every CI run.
3. A "now" publish's error says how long the platform asked to wait, so an assistant doesn't retry at once.
4. When the asks were longer than the policy allows and the retries ran out, the final error says so.
5. ``.env.example``'s retry comment mentions Retry-After.

Each ``Finding`` test failed on main before the fix committed with it; the ``Held`` tests pin what stays."""

import asyncio
import pathlib
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.config import RetryPolicy
from inkcast.libs.content.story import parse_story
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, PlatformError
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.services.publishing.sync import GitSync

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = "github.com/acme/blog"
NOW_STORY = "---\ntitle: Now\nplatforms: [devto]\n---\n\nBody.\n"


class Dev:
    """A DEV API that answers each article POST with a 429 carrying ``retry_after`` while ``limited`` is set, and
    201 otherwise. Counts the sends."""

    def __init__(self, retry_after: str | None = "900"):
        self.retry_after = retry_after
        self.limited = True
        self.sends = 0
        self.platform = DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(self)))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and request.url.path == "/api/articles":
            self.sends += 1
            if self.limited:
                headers = {"Retry-After": self.retry_after} if self.retry_after is not None else {}
                return httpx2.Response(429, headers=headers, json={"error": "Rate limit reached"})
            return httpx2.Response(201, json={"id": self.sends, "url": f"https://dev.to/on-call/{self.sends}",
                                              "published": True})
        return httpx2.Response(404, json={"error": "not found"})


class Harness(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.store.save_credentials(self.user, "devto", {"api_key": "k"}, {"id": "1", "name": "on-call"})

    def publisher(self, dev: Dev, policy: RetryPolicy | None = None) -> Publisher:
        if policy is None:
            return Publisher(self.store, {"devto": dev.platform})
        return Publisher(self.store, {"devto": dev.platform}, retry=policy)


class FindingGitSyncNowPostsAreRetriedByTheScheduler(Harness):
    def sync(self, git: GitSync, markdown: str = NOW_STORY):
        req = SyncRequest(source=SOURCE, files=[SyncFile(path="posts/now.md", markdown=markdown)])
        (result,) = asyncio.run(git.sync(self.user, req)).results
        return result

    def posts(self) -> list[dict]:
        return [dict(r) for r in self.store._all("SELECT * FROM posts ORDER BY created_at")]

    def test_a_429_on_a_now_post_schedules_its_retry_as_asked_and_the_next_sync_waits(self):
        dev = Dev("900")
        publisher = self.publisher(dev)
        git = GitSync(self.store, {"devto": dev.platform}, publisher)
        first = self.sync(git)
        self.assertEqual((first.action, first.status, dev.sends), ("create", "scheduled", 1))
        self.assertIn("Will retry", first.message)
        (post,) = self.posts()
        self.assertEqual((post["id"], post["status"], post["attempts"]), (first.post_id, "scheduled", 1))
        self.assertAlmostEqual(post["run_at"] - post["updated_at"], 900, delta=2, msg="the 429's Retry-After")
        entry = self.store.get_entry(self.user, SOURCE, "posts/now.md", "devto")
        self.assertEqual((entry["status"], entry["post_id"]), ("scheduled", first.post_id))
        # The next CI runs, during the wait: nothing new is sent or created.
        for _ in range(2):
            again = self.sync(git)
            self.assertEqual((again.action, again.status, again.post_id), ("unchanged", "scheduled", first.post_id))
        self.assertEqual((dev.sends, len(self.posts())), (1, 1))
        # The scheduler retries at the asked time; the platform has recovered; the next sync records the URL.
        asyncio.run(publisher.run_due(now=time.time() + 899))
        self.assertEqual(dev.sends, 1, "retried before the platform asked")
        dev.limited = False
        asyncio.run(publisher.run_due(now=time.time() + 901))
        self.assertEqual((dev.sends, self.store.get_post(first.post_id)["status"]), (2, "published"))
        done = self.sync(git)
        self.assertEqual((done.action, done.status, done.url), ("unchanged", "published", "https://dev.to/on-call/2"))
        self.assertEqual(len(self.posts()), 1)

    def test_when_every_retry_fails_the_next_sync_starts_over_paced_by_the_policy(self):
        dev = Dev("5")
        publisher = self.publisher(dev, RetryPolicy(3, (30, 60)))
        git = GitSync(self.store, {"devto": dev.platform}, publisher)
        first = self.sync(git)
        self.assertEqual((first.status, dev.sends), ("scheduled", 1))
        for _ in range(2):
            run_at = self.store.get_post(first.post_id)["run_at"]
            asyncio.run(publisher.run_due(now=run_at + 1))
        self.assertEqual((dev.sends, self.store.get_post(first.post_id)["status"]), (3, "failed"))
        again = self.sync(git)                                 # the next CI run: a fresh post, with its own retries
        self.assertEqual((again.action, again.status, dev.sends), ("create", "scheduled", 4))
        self.assertNotEqual(again.post_id, first.post_id)

    def test_a_definite_failure_on_a_now_post_is_still_failed_at_once(self):
        def refused(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(422, json={"error": "Title can't be blank"})

        devto = DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(refused)))
        publisher = Publisher(self.store, {"devto": devto})
        git = GitSync(self.store, {"devto": devto}, publisher)
        result = self.sync(git)
        self.assertEqual((result.action, result.status), ("failed", None))
        self.assertIn("Title can't be blank", result.message)


class HeldANowPublishByAPersonIsNotRetried(Harness):
    def test_a_429_on_a_now_publish_fails_at_once_as_before(self):
        """MCP and REST "now" posts have a person (or an assistant) to tell: no retry, the message says when."""
        dev = Dev("900")
        publisher = self.publisher(dev)
        story = parse_story(NOW_STORY, known_platforms=["devto"])
        (outcome,) = asyncio.run(publisher.publish(self.user, story, ["devto"]))
        self.assertEqual((outcome.status, dev.sends), ("failed", 1))
        asyncio.run(publisher.run_due(now=time.time() + 901))
        self.assertEqual(dev.sends, 1)


class FindingTheMessageSaysHowLongToWait(Harness):
    def send(self, dev: Dev) -> PlatformError:
        prepared = dev.platform.prepare({"title": "t"}, "Body.")
        with self.assertRaises(PlatformError) as caught:
            asyncio.run(dev.platform.publish({"api_key": "k"}, Account(id="1", name="n"), prepared, None))
        return caught.exception

    def test_the_ask_is_worded_in_the_error(self):
        for retry_after, wording in (("5", "in 5 seconds"), ("1", "in 1 second"), ("90", "in 90 seconds"),
                                     ("120", "in 2 minutes"), ("900", "in 15 minutes"), ("901", "in 16 minutes"),
                                     ("7200", "in 2 hours"), ("86400", "in 24 hours"), ("172800", "in 2 days"),
                                     ("0", "shortly"), ("soon", "shortly"), (None, "shortly")):
            with self.subTest(retry_after=retry_after):
                exc = self.send(Dev(retry_after))
                self.assertIn(f"try again {wording}", str(exc))
                self.assertIn("rate limit", str(exc))
                self.assertEqual((exc.retryable, exc.uncertain), (True, False))

    def test_a_now_publish_reports_the_ask_to_the_person(self):
        dev = Dev("86400")
        story = parse_story(NOW_STORY, known_platforms=["devto"])
        (outcome,) = asyncio.run(self.publisher(dev).publish(self.user, story, ["devto"]))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("try again in 24 hours", outcome.message)
        self.assertIn("try again in 24 hours", self.store.get_post(outcome.post_id)["error"])


class FindingTheFinalErrorSaysTheAskWasBeyondThePolicy(Harness):
    def exhaust(self, dev: Dev, policy: RetryPolicy | None = None) -> dict:
        publisher = self.publisher(dev, policy)
        prepared = dev.platform.prepare({"title": "t"}, "Body.")
        post_id = self.store.create_post(group_id="g", user_id=self.user, platform="devto", title="t",
                                         status="scheduled", run_at=time.time() - 1, payload=prepared.to_dict())
        for _ in range(20):
            post = self.store.get_post(post_id)
            if post["status"] != "scheduled":
                break
            asyncio.run(publisher.run_due(now=post["run_at"] + 1))
        return self.store.get_post(post_id)

    def test_an_ask_beyond_the_cap_is_named_in_the_final_error(self):
        post = self.exhaust(Dev("86400"))                                 # a day, under the default cap of 1800 s
        self.assertEqual((post["status"], post["attempts"]), ("failed", 4))
        self.assertIn("try again in 24 hours", post["error"])
        self.assertIn("INKCAST_PUBLISH_RETRY_DELAYS", post["error"])
        self.assertIn("1800", post["error"])
        post = self.exhaust(Dev("100"), RetryPolicy(3, (45, 90)))        # 100 s, over this policy's cap of 90 s
        self.assertEqual((post["status"], post["attempts"]), ("failed", 3))
        self.assertIn("INKCAST_PUBLISH_RETRY_DELAYS", post["error"])
        self.assertIn("90", post["error"])

    def test_an_ask_within_the_policy_and_no_ask_keep_the_plain_error(self):
        for dev in (Dev("900"), Dev("1800"), Dev(None), Dev("soon")):
            with self.subTest(retry_after=dev.retry_after):
                self.setUp()
                post = self.exhaust(dev)
                self.assertEqual(post["status"], "failed")
                self.assertNotIn("INKCAST_PUBLISH_RETRY_DELAYS", post["error"])


class FindingTheEnvExampleMentionsRetryAfter(unittest.TestCase):
    def test_the_retry_comment_mentions_retry_after(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        lines = text.splitlines()
        at = next(i for i, line in enumerate(lines) if line.startswith("# INKCAST_PUBLISH_RETRY_DELAYS="))
        comment = "\n".join(lines[max(0, at - 6):at])
        self.assertIn("Retry-After", comment)


if __name__ == "__main__":
    unittest.main()
