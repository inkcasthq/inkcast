# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #188, item 1: a 429 on ``find_existing`` during recovery must not mark a never-sent post unconfirmed.

Recovery asks the platform whether a post left 'publishing' exists. ``confirm`` treated any ``PlatformError`` as
"can't tell", so a rate-limited (or unreachable) lookup ended the post unconfirmed: a batch tail taken as stuck on a
rate-limiting platform was never sent, and a person was told to check by hand. A lookup that failed for a reason
that will pass (a 429, a connection failure, a 5xx on a GET: ``retryable``) now leaves the post 'recovering', and
the next stuck pass asks again; a definite answer (401, no ``find`` capability, a plug-in bug) is unconfirmed as
before. Git sync's check of an unconfirmed entry says the platform couldn't be asked just now, and asks again on
the next sync.

Each ``Finding`` test failed on main before the fix committed with it."""

import asyncio
import json
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.libs.content.story import parse_story
from inkcast.services.publishing.sync import GitSync, payload_hash

SOURCE = "github.com/acme/blog"


class Dev:
    """A DEV API whose article listing answers as told (``lookup``: a status, or a list of articles), and whose
    article POST succeeds. Records every request."""

    def __init__(self):
        self.lookup: int | list[dict] = 429
        self.requests: list[httpx2.Request] = []
        self.articles: list[dict] = []
        self.platform = DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(self)))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == "/api/articles/me/all":
            if isinstance(self.lookup, int):
                return httpx2.Response(self.lookup, headers={"Retry-After": "900"}, json={"error": "no"})
            return httpx2.Response(200, json=self.lookup)
        if request.method == "POST" and request.url.path == "/api/articles":
            article = json.loads(request.content)["article"]
            self.articles.append(article)
            return httpx2.Response(201, json={"id": 1000 + len(self.articles), "url": "https://dev.to/on-call/t",
                                              "published": True})
        return httpx2.Response(404, json={"error": "not found"})

    def lookups(self) -> int:
        return sum(r.url.path == "/api/articles/me/all" for r in self.requests)


class Recovery(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.dev = Dev()
        self.store.save_credentials(self.user, "devto", {"api_key": "k"}, {"id": "1", "name": "on-call"})
        self.publisher = Publisher(self.store, {"devto": self.dev.platform})

    def stuck(self, title: str = "Stuck") -> str:
        """A post the process was sending when it stopped: 'publishing' for longer than STUCK_AFTER."""
        prepared = self.dev.platform.prepare({"title": title}, "Body.")
        post_id = self.store.create_post(group_id="g", user_id=self.user, platform="devto", title=title,
                                         status="publishing", run_at=time.time() - 1, payload=prepared.to_dict())
        self.age()
        return post_id

    def age(self):
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?", STUCK_AFTER + 1)

    def tick(self):
        with self.assertLogs("inkcast.libs.pipeline", "WARNING"):
            asyncio.run(self.publisher.run_due())


class FindingATransientLookupFailureKeepsThePostRecovering(Recovery):
    def test_a_429_on_the_lookup_is_asked_again_on_the_next_pass_and_the_post_is_then_sent(self):
        post_id = self.stuck()
        self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], self.dev.lookups(), len(self.dev.articles)), ("recovering", 1, 0))
        self.assertIn("rate limit", post["error"])
        # Not asked again before the next stuck pass.
        asyncio.run(self.publisher.run_due())
        self.assertEqual(self.dev.lookups(), 1)
        # The platform answers on the next pass: nothing there, so the post goes back in the queue and is sent once.
        self.dev.lookup = []
        self.age()
        self.tick()
        self.assertEqual(self.store.get_post(post_id)["status"], "scheduled")
        asyncio.run(self.publisher.run_due())
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], len(self.dev.articles)), ("published", 1))

    def test_a_post_the_platform_turns_out_to_have_is_confirmed_on_the_later_pass(self):
        post_id = self.stuck("Was sent")
        self.tick()
        self.assertEqual(self.store.get_post(post_id)["status"], "recovering")
        self.dev.lookup = [{"id": 77, "title": "Was sent", "url": "https://dev.to/on-call/was-sent", "published": True}]
        self.age()
        self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], post["remote_id"], len(self.dev.articles)),
                         ("published", "https://dev.to/on-call/was-sent", "77", 0))

    def test_every_transient_lookup_failure_defers_and_every_definite_one_is_unconfirmed(self):
        for lookup, expected in ((429, "recovering"), (503, "recovering"), (401, "unconfirmed"), (404, "unconfirmed")):
            with self.subTest(lookup=lookup):
                self.setUp()
                self.dev.lookup = lookup
                post_id = self.stuck()
                self.tick()
                self.assertEqual(self.store.get_post(post_id)["status"], expected)
        # The connection never made: as transient as a 429.
        self.setUp()

        def down(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused")

        self.publisher = Publisher(self.store, {"devto": DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(down)))})
        post_id = self.stuck()
        self.tick()
        self.assertEqual(self.store.get_post(post_id)["status"], "recovering")

    def test_the_rest_of_the_stuck_batch_is_still_resolved_in_the_same_pass(self):
        first, second = self.stuck("A"), self.stuck("B")
        self.tick()
        self.assertEqual([self.store.get_post(p)["status"] for p in (first, second)], ["recovering", "recovering"])
        self.assertEqual(self.dev.lookups(), 2)


class HeldWhatRecoveryStillDoes(Recovery):
    def test_a_platform_that_cannot_be_asked_at_all_is_unconfirmed_at_once(self):
        from inkcast.libs.platforms.medium import MediumPlatform
        medium = MediumPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(self.dev)))
        self.store.save_credentials(self.user, "medium", {"token": "t"}, {"id": "1", "name": "on-call"})
        self.publisher = Publisher(self.store, {"medium": medium})
        prepared = medium.prepare({"title": "t"}, "Body.")
        post_id = self.store.create_post(group_id="g", user_id=self.user, platform="medium", title="t",
                                         status="publishing", run_at=time.time() - 1, payload=prepared.to_dict())
        self.age()
        self.tick()
        self.assertEqual(self.store.get_post(post_id)["status"], "unconfirmed")


class SyncCheck(Recovery):
    def sync(self, markdown: str):
        git = GitSync(self.store, {"devto": self.dev.platform}, self.publisher)
        req = SyncRequest(source=SOURCE, files=[SyncFile(path="posts/u.md", markdown=markdown)])
        (result,) = asyncio.run(git.sync(self.user, req)).results
        return result


class FindingGitSyncSaysThePlatformCouldNotBeAskedJustNow(SyncCheck):
    def test_an_unconfirmed_entry_whose_check_is_rate_limited_is_checked_again_on_the_next_sync(self):
        markdown = "---\ntitle: Uncertain\nplatforms: [devto]\n---\n\nBody.\n"
        # The last publish ended unconfirmed (a 502 after the request was sent, say).
        self.store.claim_new(self.user, SOURCE, "posts/u.md", "devto", "h")
        self.store.put_entry(self.user, SOURCE, "posts/u.md", "devto", status="unconfirmed", content_hash="h")
        first = self.sync(markdown)
        self.assertEqual((first.action, first.status, len(self.dev.articles)), ("skipped", "unconfirmed", 0))
        self.assertIn("just now", first.message)
        self.assertNotIn("remove this file's ledger entry", first.message, "a wait, not a job for a person")
        self.assertEqual(self.store.get_entry(self.user, SOURCE, "posts/u.md", "devto")["status"], "unconfirmed")
        # The next CI run: DEV answers, the article isn't there, and it is created exactly once.
        self.dev.lookup = []
        second = self.sync(markdown)
        self.assertEqual((second.action, len(self.dev.articles)), ("create", 1))
        self.assertEqual(self.sync(markdown).action, "unchanged")


class HeldGitSyncStillAsksAPersonWhenThePlatformCannotTell(SyncCheck):
    def test_a_definite_lookup_failure_still_asks_a_person(self):
        markdown = "---\ntitle: Uncertain\nplatforms: [devto]\n---\n\nBody.\n"
        # The entry names no post and holds the hash of this very file: an unchanged file waits for the person (#226:
        # an edited one is published as a new post, since there is no post to decide on).
        story = parse_story(markdown, known_platforms=["devto"], schedule_as_written=True)
        digest = payload_hash(self.dev.platform.prepare(story.for_platform("devto"), story.body))
        self.store.claim_new(self.user, SOURCE, "posts/u.md", "devto", digest)
        self.store.put_entry(self.user, SOURCE, "posts/u.md", "devto", status="unconfirmed", content_hash=digest)
        self.dev.lookup = 401
        result = self.sync(markdown)
        self.assertEqual((result.action, result.status, len(self.dev.articles)), ("skipped", "unconfirmed", 0))
        self.assertIn("can't be checked automatically", result.message)
        self.assertIn("published as a new post", result.message)


if __name__ == "__main__":
    unittest.main()
