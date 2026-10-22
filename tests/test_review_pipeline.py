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
from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, Prepared, PublishResult


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


if __name__ == "__main__":
    unittest.main()
