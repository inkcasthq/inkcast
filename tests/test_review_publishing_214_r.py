# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #390 (issue #214): the writes after a send, and an empty 2xx.

Each ``Finding…`` class failed on the PR's head (bdecbf2) before the fix committed with it."""

import asyncio
import time
import unittest

from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import PlatformError, PublishResult

from test_review_publishing_214 import Findable, Harness, answering

DAY = 24 * 3600


class FindingALateSuccessIsLostWhenTheReSendNeverTookEffect(Harness):
    """The PR fences the success write with the attempt count alone: once another worker has re-sent the post, A's late
    2xx is only logged. But B's re-send may have certainly never taken effect: a retryable failure (the request never
    reached the platform; the post waits for its retry), a definite one ('failed'), or the person cancelled the post
    that waited for its retry. A's copy is then the only one on the platform, and the row says it is not out: the
    retry posts it a second time, a 'failed' post is the person's (or Git sync's) to publish again, and a cancelled one
    has no URL for the person to remove it by (#216). On main the success was written in all three."""

    def late_success_after(self, b_answer: PlatformError, cancel: bool = False) -> tuple[str, object, list]:
        post_id = self.post()
        b = self.replica()
        seen = []

        async def b_never_arrives(prepared):                                  # inside B's publish: refused before it lands
            raise b_answer

        async def while_a_waits(prepared):                                    # inside A's publish, A's request out
            self.platform.during_publish = b_never_arrives
            await b.run_due(now=time.time() + STUCK_AFTER + 1)                # B: missing → re-queued → sent → b_answer
            if cancel:
                b.cancel(self.user, post_id)                                  # the person cancels the post waiting to retry
            post = self.store.get_post(post_id)
            seen.append((post["status"], post["attempts"]))

        self.platform.during_publish = while_a_waits
        (outcome,) = asyncio.run(self.publisher.run_due(now=time.time()))    # A's tick: its 2xx lands last
        return post_id, outcome, seen

    def test_a_success_over_a_re_send_queued_for_its_retry_is_written_and_the_post_is_not_sent_again(self):
        post_id, outcome, seen = self.late_success_after(PlatformError("Findable is unreachable.", retryable=True))
        self.assertEqual(seen, [("scheduled", 2)])
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], post["result_url"]), ("published", "published", "https://findable/1"),
                         "A's success, the only copy, was dropped")
        asyncio.run(self.publisher.run_due(now=time.time() + DAY))           # long after any retry delay
        self.assertEqual(self.platform.sent, ["t"], "posted twice")

    def test_a_success_over_a_re_send_that_failed_is_written(self):
        post_id, outcome, seen = self.late_success_after(PlatformError("Findable refused the post (422)."))
        self.assertEqual(seen, [("failed", 2)])
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], post["result_url"]), ("published", "published", "https://findable/1"),
                         "the post is out, and reads 'failed'")

    def test_a_success_over_a_cancel_of_the_re_send_waiting_for_its_retry_is_written(self):
        post_id, outcome, seen = self.late_success_after(PlatformError("Findable is unreachable.", retryable=True),
                                                         cancel=True)
        self.assertEqual(seen, [("cancelled", 2)])
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"]), ("published", "https://findable/1"),
                         "the person has no URL to remove the post by (#216)")


class FindingAnEmptyAnswerToAnUpdateErasesThePostsURL(unittest.TestCase):
    """Item 2's other half: ``Publisher.update`` (Git sync's edit of a published post, a PUT) wrote ``result_url=None``
    when the answer named no URL. A 204 is a PUT's own success (RFC 9110 §15.3.5: "the action has been successfully
    applied", its example a PUT), so the update is right to read it as done; but the post's known URL was erased from
    the row (the ledger kept it: ``o.url or entry["url"]``), and the person's list showed a published post with none."""

    def test_a_204_or_an_empty_body_to_an_update_keeps_the_posts_url(self):
        for status, body in ((204, b""), (200, b"{}")):
            (devto, _medium), requests = answering(status, body)
            with self.subTest(status=status, body=body):
                store = Store(":memory:", "k" * 40)
                self.addCleanup(store.close)
                user = store.upsert_user("local:on-call", "on-call", None, None)["id"]
                store.save_credentials(user, devto.id, {"api_key": "k"}, {"id": "1", "name": "on-call"})
                prepared = devto.prepare({"title": "t"}, "Body.")
                post_id = store.create_post(group_id="g", user_id=user, platform=devto.id, title="t",
                                            status="publishing", run_at=time.time(), payload=prepared.to_dict())
                store.update_post(post_id, status="published", result_url="https://dev.to/on-call/t", remote_id="7")
                outcome = asyncio.run(Publisher(store, {devto.id: devto}).update(user, post_id, devto, prepared, "7"))
                post = store.get_post(post_id)
                self.assertEqual((requests[-1].method, outcome.status, outcome.remote_id), ("PUT", "published", "7"))
                self.assertEqual((post["status"], post["result_url"], post["remote_id"]),
                                 ("published", "https://dev.to/on-call/t", "7"), "the post's URL was erased")


class ProfileURL(Findable):
    """A platform whose answer names each post by its id, and by a URL that is not the post's own (the account's
    profile, say): the id is the post's identity, the URL is not."""

    async def publish(self, creds, account, prepared, destination):
        result = await super().publish(creds, account, prepared, destination)
        return PublishResult(url="https://findable/@on-call", remote_id=result.remote_id, status=result.status)


class FindingTwoCopiesSharingAURLReadAsOne(Harness):
    """``unless_other_copy`` (posts.py) calls the row's copy this answer's when either the remote id or the URL
    matches. Two posts with distinct ids and the same URL were then one copy: worker B's re-send published (id 1),
    worker A's late 2xx (id 2) was written over it, B's id was lost from the row with no note naming it, and a
    second post.published event was written for one post (libs/events.py: "never twice"). Ids, when both answers
    have one, are what tell two posts apart; the URL is compared only when one of them has none."""

    def test_a_late_success_with_another_id_is_another_copy_though_the_url_is_the_same(self):
        self.platform = ProfileURL()
        self.platforms[self.platform.id] = self.platform
        self.publisher = Publisher(self.store, self.platforms)
        post_id = self.post()
        b = self.replica()

        async def while_a_waits(prepared):                                    # inside A's publish
            await b.run_due(now=time.time() + STUCK_AFTER + 1)                # B: missing → re-queued → published (id 1)

        self.platform.during_publish = while_a_waits
        (outcome,) = asyncio.run(self.publisher.run_due(now=time.time()))    # A's 2xx (id 2) lands last
        post = self.store.get_post(post_id)
        self.assertEqual(self.platform.sent, ["t", "t"])
        self.assertEqual((post["status"], post["remote_id"], outcome.remote_id), ("published", "1", "1"),
                         "A's copy was written over B's: B's id is lost")
        self.assertIn("Attempt 1 also published this post", post["error"])
        self.assertEqual(self.published_events(post_id), 1, "one post, one post.published event")


if __name__ == "__main__":
    unittest.main()
