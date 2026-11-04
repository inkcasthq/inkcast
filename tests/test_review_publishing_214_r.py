# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #390 (issue #214): the writes after a send, and an empty 2xx.

Each ``Finding…`` class failed on the PR's head (bdecbf2) before the fix committed with it."""

import asyncio
import time
import unittest

from inkcast.libs.pipeline import STUCK_AFTER
from inkcast.libs.platforms import PlatformError

from test_review_publishing_214 import Harness

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


if __name__ == "__main__":
    unittest.main()
