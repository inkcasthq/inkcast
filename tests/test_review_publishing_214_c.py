# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #390 (issue #214), third pass: 96a0213, a send's success recorded in one transaction.

Each ``Finding…`` class failed on 96a0213 before the fix committed with it."""

import asyncio
import time
import unittest

from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import PublishResult

from test_review_publishing_214 import Findable, Harness


class IntIds(Findable):
    """A plug-in that hands on the platform's numeric id as it came in the JSON (DEV's API answers ``"id": 7``;
    devto.py turns it into a string, a plug-in may not). The posts table stores it as text (``remote_id TEXT``)."""

    async def publish(self, creds, account, prepared, destination):
        result = await super().publish(creds, account, prepared, destination)
        return PublishResult(url=result.url, remote_id=int(result.remote_id), status=result.status)

    async def find_existing(self, creds, account, prepared):
        found = await super().find_existing(creds, account, prepared)
        return found and PublishResult(url=found.url, remote_id=int(found.remote_id), status=found.status)


class FindingANumericIdIsAnotherCopyOfItself(Harness):
    """``PostsRepo.same_copy`` compares the row's id with the answer's in Python: the row holds the text '1', the answer
    the number 1, and they differ. 57c80fc compared them in SQL, where the column's text affinity makes them equal. On
    96a0213 a post a lookup recorded first, then named by its own send's 2xx, read as two copies: the row's error says
    the platform may have the post twice, naming the same URL and id on both sides, and the post.published event of
    the send's own success is never written."""

    def test_a_success_naming_the_lookups_copy_by_a_numeric_id_is_the_same_copy_recorded_again(self):
        self.platform = IntIds()
        self.platforms[self.platform.id] = self.platform
        self.publisher = Publisher(self.store, self.platforms)
        post_id = self.post()
        b = self.replica()

        async def while_a_waits(prepared):
            self.platform.sent.append(prepared.title)                         # A's request has reached the platform
            await b.run_due(now=time.time() + STUCK_AFTER + 1)                # B: recovering → found (id 1) → published
            self.platform.sent.pop()                                          # A's own publish appends it below

        self.platform.during_publish = while_a_waits
        (outcome,) = asyncio.run(self.publisher.run_due(now=time.time()))    # A's 2xx names the same post, id 1
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], post["remote_id"], post["result_url"]),
                         ("published", "published", "1", "https://findable/1"))
        self.assertIsNone(post["error"], "one copy named as two")
        self.assertIsNone(outcome.message)
        self.assertEqual(self.published_events(post_id), 1, "the send's own success has no post.published event")


if __name__ == "__main__":
    unittest.main()
