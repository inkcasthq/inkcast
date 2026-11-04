# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Integration of PR #454 (#445), from the OB3 review's concerns: the refused resend's reply states the wait once.

The platform's own 429 already says when to try again (``Platform.request``: "rate limit hit; try again in 15
minutes."), and the refusal added "Try again in 900 s, as Findable's Retry-After asks": one wait, twice, in two units.
A connection error's text has no full stop of its own, so the refusal read "...Connection refused Post pst_...". The
errors here are worded as ``Platform.request`` words them."""

import asyncio
import unittest

from inkcast.libs.platforms import PlatformError
from inkcast.libs.platforms.base import wait_text
from inkcast.services.publishing.usecases import ServiceError

from test_resend_retryable_verify_445 import RateLimitedCheck


class FindingTheRefusalStatesTheWaitOnce(RateLimitedCheck):
    def refused(self, error):
        old_id = self.unconfirmed()
        self.platform.verify_answer = error
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        return old_id, caught.exception.message

    def test_a_429_names_its_wait_in_the_words_the_platform_error_uses(self):
        old_id, message = self.refused(PlatformError(f"Findable rate limit hit; try again {wait_text(900)}.",
                                                     retryable=True, retry_after=900))
        self.assertEqual(message, "Findable rate limit hit; try again in 15 minutes. "
                                  f"Post {old_id} was not resent and is still unconfirmed: resend it in 15 minutes.")

    def test_a_connection_error_ends_its_sentence_before_the_post_is_named(self):
        old_id, message = self.refused(PlatformError("Could not reach Findable: [Errno 61] Connection refused",
                                                     retryable=True))
        self.assertEqual(message, "Could not reach Findable: [Errno 61] Connection refused. "
                                  f"Post {old_id} was not resent and is still unconfirmed: resend it shortly.")


if __name__ == "__main__":
    unittest.main()
