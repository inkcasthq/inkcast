# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of the publishing pipeline: content parsing, the scheduler, Git sync and the ledger.

Each test forces the input or the interleaving that used to break, and failed before the fix committed with it."""

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.libs.content.story import StoryError, parse_story
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.db.ledger import CLAIM_TTL
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Destination, Platform, Prepared, PublishResult
from inkcast.services.publishing.sync import GitSync


class Fake(Platform):
    """A platform that records every publish, and can find what it was sent (``find``)."""

    id, name, default_api_url, credential_fields = "fake", "Fake", "https://fake.invalid", []
    capabilities = frozenset({"find"})

    def __init__(self, http=None):
        super().__init__(http or httpx2.AsyncClient())
        self.sent: list[str] = []
        self.on_publish = None  # an async hook run inside publish(), after the request "reached" the platform

    async def verify(self, creds):
        return Account(id="1", name="fake")

    def prepare(self, fields, body):
        return Prepared(platform="fake", title=fields["title"], payload={"body": body}, preview={},
                        destination=fields.get("destination"))

    async def publish(self, creds, account, prepared, destination):
        self.sent.append(prepared.title)
        if self.on_publish:
            await self.on_publish(prepared)
        return PublishResult(url=f"https://fake/{len(self.sent)}", remote_id=str(len(self.sent)), status="published")

    async def find_existing(self, creds, account, prepared):
        if prepared.title in self.sent:
            return PublishResult(url="https://fake/found", remote_id="found", status="published")
        return None

    async def resolve_destination(self, creds, account, wanted):
        await asyncio.sleep(0)            # a network round trip: other requests run meanwhile
        return Destination(id=wanted, name=wanted)


def shared_db() -> str:
    """One SQLite file that several Store connections (several replicas) open."""
    return tempfile.mkdtemp() + "/inkcast.db"


class Died(BaseException):
    """The process stopped right here (OOM kill, node drain, deploy): nothing after this line runs."""


def story_md(title: str = "Hello", body: str = "Body.", extra: str = "") -> str:
    return f"---\ntitle: {title}\nplatforms: [fake]\n{extra}---\n\n{body}\n"


class SyncHarness:
    """A store with one user connected to the fake platform, its publisher and Git sync."""

    def make(self, platform: Platform | None = None):
        self.store = Store(shared_db(), "k" * 40)
        self.store.upsert_user("local:usr", "usr", None, None)
        self.user = self.store._one("SELECT id FROM users")["id"]
        self.fake = platform or Fake()
        self.store.save_credentials(self.user, "fake", {"k": "v"}, {"id": "1", "name": "fake"})
        self.publisher = Publisher(self.store, {"fake": self.fake})
        self.git = GitSync(self.store, {"fake": self.fake}, self.publisher)

    def sync(self, markdown: str, path: str = "posts/p.md"):
        req = SyncRequest(source="github.com/acme/blog", files=[SyncFile(path=path, markdown=markdown)])
        return asyncio.run(self.git.sync(self.user, req)).results

    def age(self):
        """Ten minutes and a second later: claims and 'publishing' rows from before now look abandoned."""
        self.store._exec("UPDATE ledger SET synced_at = synced_at - ?", CLAIM_TTL + 1)
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?", STUCK_AFTER + 1)


class FrontMatterAliasBomb(unittest.TestCase):
    def test_yaml_aliases_in_front_matter_are_refused_not_expanded(self):
        """Nine aliases per level, seven levels: under 400 bytes that become a 25 MB tag (two more levels: 2 GB)
        as soon as anything turns the value into text. Any posts:read token can send it to /api/v1/preview."""
        names = "abcdefg"
        lines = ["a: &a [x, x, x, x, x, x, x, x, x]"]
        lines += [f"{n}: &{n} [{', '.join(['*' + prev] * 9)}]" for prev, n in zip(names, names[1:])]
        markdown = "---\n" + "\n".join(lines) + "\ntitle: Bomb\ntags: [*g]\n---\n\nBody.\n"
        self.assertLess(len(markdown), 400)
        with self.assertRaisesRegex(StoryError, "alias"):
            parse_story(markdown, known_platforms=["devto"])
        # Anchors alone (no alias refers to them) are harmless and still parse.
        story = parse_story("---\ntitle: &t Fine\ntags: [a]\n---\n\nBody.\n", known_platforms=["devto"])
        self.assertEqual((story.title, story.tags), ("Fine", ["a"]))


class SchedulerReplicas(unittest.TestCase):
    def test_a_batch_post_taken_over_as_stuck_is_not_sent_again_by_the_worker_that_claimed_it(self):
        """``claim_due_posts`` marks a whole batch 'publishing' at once, then sends it one post at a time. When the
        platform is slow (30 s timeouts, 20 posts a batch) the tail waits past STUCK_AFTER, so another replica's
        ``claim_stuck`` takes it over, finds it missing, re-queues and sends it; then the first worker sends it too."""
        path = shared_db()
        one, two = Store(path, "k" * 40), Store(path, "k" * 40)       # two scheduler replicas, one database
        one.save_credentials("usr", "fake", {"k": "v"}, {"id": "1", "name": "fake"})
        fake = Fake()
        first, second = Publisher(one, {"fake": fake}), Publisher(two, {"fake": fake})
        for title in ("slow", "tail"):
            one.create_post(group_id="g", user_id="usr", platform="fake", title=title, status="scheduled",
                            run_at=time.time() - 1, payload={"platform": "fake", "title": title, "payload": {},
                                                             "preview": {}})

        async def meanwhile(prepared):
            if prepared.title == "slow":   # the platform takes its time; the other replica ticks STUCK_AFTER later
                await second.run_due(now=time.time() + STUCK_AFTER + 1)

        fake.on_publish = meanwhile
        asyncio.run(first.run_due())
        self.assertEqual(fake.sent.count("slow"), 1)
        self.assertEqual(fake.sent.count("tail"), 1, fake.sent)
        self.assertEqual({r["status"] for r in one._all("SELECT status FROM posts")}, {"published"})


class SyncDiedMidSend(SyncHarness, unittest.TestCase):
    """A sync claims the ledger entry ('syncing', no post id), creates the post and sends it. If the process stops
    before the result is recorded, the post row is recovered by the scheduler, but the ledger never learnt its id:
    after CLAIM_TTL the next sync takes the claim over and creates the post again, without asking anyone."""

    def test_the_request_reached_the_platform_and_cant_be_checked(self):
        self.make()
        self.fake.capabilities = frozenset()                      # like Medium: no find_existing

        async def died(prepared):
            raise Died()

        self.fake.on_publish = died
        with self.assertRaises(Died):
            self.sync(story_md())
        self.fake.on_publish = None
        self.age()
        self.sync(story_md())                                     # the next CI run, before the scheduler's recovery
        self.assertEqual(self.fake.sent, ["Hello"], "created again by the claim takeover")
        asyncio.run(self.publisher.run_due())                     # recovery: can't be checked → unconfirmed
        (result,) = self.sync(story_md())
        self.assertEqual((result.action, result.status), ("skipped", "unconfirmed"))
        self.assertEqual(self.fake.sent, ["Hello"])

    def test_the_request_never_left_and_the_scheduler_re_queued_it(self):
        self.make()

        async def died_before_sending(prepared):
            self.fake.sent.pop()                                   # the request never reached the platform
            raise Died()

        self.fake.on_publish = died_before_sending
        with self.assertRaises(Died):
            self.sync(story_md())
        self.fake.on_publish = None
        self.age()
        asyncio.run(self.publisher.run_due())                     # recovery: find_existing says missing → re-queued
        self.sync(story_md())                                     # a CI run before the next scheduler tick
        asyncio.run(self.publisher.run_due())
        self.assertEqual(self.fake.sent, ["Hello"], "the re-queued post and a second create both went out")


class ConcurrentResync(SyncHarness, unittest.TestCase):
    def test_two_syncs_of_an_edited_scheduled_file_leave_one_scheduled_post(self):
        """Two CI runs of the same commit (a push and a re-run): the first cancels the scheduled post and, while it
        resolves the destination, the second sees the post cancelled, so it creates one too. Both go out."""
        self.make()
        when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
        extra = f"publish_at: {when}\nfake:\n  destination: team\n"
        (first,) = self.sync(story_md(extra=extra))
        self.assertEqual((first.action, first.status), ("create", "scheduled"))
        req = SyncRequest(source="github.com/acme/blog",
                          files=[SyncFile(path="posts/p.md", markdown=story_md(body="Better body.", extra=extra))])

        async def two_ci_runs():
            return await asyncio.gather(self.git.sync(self.user, req), self.git.sync(self.user, req))

        actions = sorted(r.results[0].action for r in asyncio.run(two_ci_runs()))
        scheduled = [r["id"] for r in self.store._all("SELECT id FROM posts WHERE status = 'scheduled'")]
        self.assertEqual(len(scheduled), 1, f"scheduled twice ({actions})")
        self.assertEqual(self.store.get_entry(self.user, "github.com/acme/blog", "posts/p.md", "fake")["post_id"],
                         scheduled[0])
        self.assertEqual(actions, ["skipped", "update"])


class PluginCrashAfterSend(SyncHarness, unittest.TestCase):
    def test_a_post_the_platform_accepted_is_not_created_again_when_the_plugin_crashes_reading_the_reply(self):
        """DEV creates the article and answers 201, but in a shape the plug-in doesn't expect (here a JSON array):
        reading it raises inside publish(). That was recorded 'failed', and Git sync re-creates 'failed' entries,
        so every CI run added another copy of the article."""
        import json
        from inkcast.libs.platforms.devto import DevtoPlatform

        articles: list[dict] = []

        def dev(request: httpx2.Request) -> httpx2.Response:
            if request.method == "POST" and request.url.path == "/api/articles":
                articles.append(json.loads(request.content)["article"])
                return httpx2.Response(201, json=[{"id": 1000 + len(articles), "url": "https://dev.to/ada/x"}])
            if request.url.path == "/api/articles/me/all":
                return httpx2.Response(200, json=[{"id": 1000 + i, "title": a["title"], "url": f"https://dev.to/ada/{i}",
                                                   "canonical_url": a.get("canonical_url"), "published": a["published"]}
                                                  for i, a in enumerate(articles, 1)])
            return httpx2.Response(404, json={"error": "not found"})

        devto = DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(dev)))
        self.make()
        self.store.save_credentials(self.user, "devto", {"api_key": "k"}, {"id": "42", "name": "ada"})
        self.publisher = Publisher(self.store, {"devto": devto})
        self.git = GitSync(self.store, {"devto": devto}, self.publisher)
        markdown = "---\ntitle: Shape change\nplatforms: [devto]\n---\n\nBody.\n"
        with self.assertLogs("inkcast.libs.pipeline", "ERROR"):
            first, *_ = [self.sync(markdown)[0] for _ in range(3)]   # a push, then two more CI runs
        self.assertEqual(len(articles), 1, "one more copy of the article per sync")
        self.assertEqual(first.status, "unconfirmed", first.message)
        entry = self.store.get_entry(self.user, "github.com/acme/blog", "posts/p.md", "devto")
        self.assertEqual((entry["status"], entry["remote_id"]), ("published", "1001"))   # found on DEV and recorded


if __name__ == "__main__":
    unittest.main()
