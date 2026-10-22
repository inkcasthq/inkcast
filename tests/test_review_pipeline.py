# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of the publishing pipeline: content parsing, the scheduler, Git sync and the ledger.

Each test forces the input or the interleaving that used to break, and failed before the fix committed with it."""

import asyncio
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.content.story import StoryError, parse_story
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.db.ledger import CLAIM_TTL
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, Prepared, PublishResult
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


if __name__ == "__main__":
    unittest.main()
