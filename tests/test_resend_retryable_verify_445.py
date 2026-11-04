# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #445: a resend whose credentials check can't be made just now is refused, before anything is cancelled.

``Publisher.resend`` (PR #442, #234 item 4) checks the platform's credentials (``verify``) before it cancels the old
post, and refused the resend on a non-retryable error only: a retryable one (a 429 on the account lookup) let it
through. The send that followed then very likely got the same 429, and a resend's send is never queued (``retry=False``):
the old post was cancelled and the new one ended 'failed', the outcome the check was there to prevent. Now a retryable
verify error refuses the resend too (``Unavailable``, 409 ``platform_unavailable``): nothing is cancelled, the post stays
'unconfirmed', and the message names when to try again, from the platform's Retry-After when it gave one.
``FindingARetryableVerifyErrorRefusesTheResend`` failed on main (no refusal: the old post 'cancelled', the new one
'failed'); ``FindingTheRefusalIsOnlyForNow`` (failed on main too: the first resend went through) has the same post
resent once the platform answers."""

import asyncio
import unittest

from inkcast.libs.platforms import PlatformError
from inkcast.services.publishing.usecases import ServiceError

from test_person_decides import Harness
from test_person_decides_followups import Refusing


class RateLimitedCheck(Harness):
    platform_class = Refusing

    def rate_limited(self, retry_after=900):
        """The platform rate-limits the account lookup and, as is likely, the send that would follow it."""
        self.platform.verify_answer = PlatformError("Findable rate limit hit.", retryable=True, retry_after=retry_after)
        self.platform.answer = PlatformError("Findable rate limit hit.", retryable=True, retry_after=retry_after)


class FindingARetryableVerifyErrorRefusesTheResend(RateLimitedCheck):
    def test_nothing_is_cancelled_the_post_stays_unconfirmed_and_the_reply_names_the_wait(self):
        old_id = self.unconfirmed()
        self.rate_limited(retry_after=900)
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))               # on main: went through, and failed
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "platform_unavailable"))
        message = caught.exception.message
        self.assertIn("Findable rate limit hit.", message)
        self.assertIn(f"Post {old_id} was not resent and is still unconfirmed", message)
        self.assertIn("resend it in 15 minutes.", message, "the platform's Retry-After is not named")
        self.assertEqual((self.status(old_id), len(self.posts()), self.platform.sent, self.platform.verified),
                         ("unconfirmed", 1, [], 1), "something was cancelled or sent")
        self.assertEqual([t for t, _, _ in self.events(old_id)], [], "a decision was recorded")
        self.assertIsNotNone(self.platform.answer, "the send was made")

    def test_a_retryable_error_without_a_retry_after_names_a_wait_too(self):
        old_id = self.unconfirmed()
        self.platform.verify_answer = PlatformError("Findable is unreachable.", retryable=True)
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "platform_unavailable"))
        self.assertIn("Findable is unreachable.", caught.exception.message)
        self.assertIn("resend it shortly.", caught.exception.message)
        self.assertEqual((self.status(old_id), len(self.posts()), self.platform.sent), ("unconfirmed", 1, []))


class FindingTheRefusalIsOnlyForNow(RateLimitedCheck):
    def test_the_same_post_is_resent_once_the_platform_answers(self):
        old_id = self.unconfirmed()
        self.rate_limited(retry_after=60)
        with self.assertRaises(ServiceError):
            asyncio.run(self.service.resend(self.user, old_id))
        self.platform.answer = None                                           # the wait is over
        info = asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((info.status, self.status(old_id), self.platform.sent, self.platform.verified),
                         ("published", "cancelled", ["t"], 2))


if __name__ == "__main__":
    unittest.main()
