# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #502 (OB3): what the owner of a post that was never sent is told when they decide on it."""

import asyncio

from inkcast.libs.pipeline import STUCK_AFTER

from test_person_decides import Blind
from test_wrong_key_472 import WrongKeyScheduler

MAYBE_OUT = "may already be on Blind"


class FindingTheOwnersDecisionIsNotToldTheNeverSentPostMayBeOut(WrongKeyScheduler):
    """Recovery tells the owner the post was not sent (#502); their cancel, and their resend's note on the old post,
    then said it may already be on the platform and to remove the extra copy there: a copy Inkcast knows was never made."""

    platform_class = Blind

    def never_sent(self) -> str:
        post_id = self.post("scheduled")
        self.tick(publisher=self.wrong)
        self.tick(later=STUCK_AFTER + 1, publisher=self.wrong)
        self.assertEqual(self.status(post_id), "unconfirmed")
        return post_id

    def test_the_cancel_says_it_was_never_sent(self):
        post_id = self.never_sent()
        post = self.service.cancel(self.user, post_id)
        self.assertEqual(post.status, "cancelled")
        self.assertNotIn(MAYBE_OUT, post.error)
        self.assertIn("so it was not sent", post.error)

    def test_the_resend_says_the_new_post_is_the_only_copy(self):
        post_id = self.never_sent()
        outcome = asyncio.run(self.service.resend(self.user, post_id))
        old = self.store.get_post(post_id)
        self.assertEqual((outcome.status, old["status"], self.platform.sent), ("published", "cancelled", ["t"]))
        self.assertNotIn(MAYBE_OUT, old["error"])
        self.assertIn("so it was not sent", old["error"])

    def test_an_unconfirmed_post_whose_send_began_still_says_it_may_be_out(self):
        """Held: only the never-sent record changes the words."""
        for decide in ("cancel", "resend"):
            with self.subTest(decide=decide):
                post_id = self.post("unconfirmed", error="Blind timed out after the request was sent.")
                if decide == "cancel":
                    self.service.cancel(self.user, post_id)
                else:
                    asyncio.run(self.service.resend(self.user, post_id))
                self.assertIn(MAYBE_OUT, self.store.get_post(post_id)["error"])


class HeldTheNeverSentRecordsGuards(WrongKeyScheduler):
    """The fences mutation left untested: each test fails when its guard is removed."""

    platform_class = Blind

    def test_the_record_is_not_written_over_a_later_claim(self):
        """``if_attempts``: another worker re-claimed the post (one attempt on, 'publishing' too) before the read failed."""
        post_id = self.post("scheduled")
        read = self.wrong.store.get_credentials

        def reclaimed_then_unreadable(user_id, platform_id):
            self.store._exec("UPDATE posts SET attempts = attempts + 1 WHERE id = ?", post_id)
            return read(user_id, platform_id)

        self.wrong.store.get_credentials = reclaimed_then_unreadable
        self.tick(publisher=self.wrong)
        self.assertEqual((self.status(post_id), self.store.get_post(post_id)["error"]), ("publishing", None))

    def test_a_cancel_before_the_stuck_pass_writes_wins_and_is_not_counted(self):
        """The fence of the never-sent branch's write, and its count only when written."""
        post_id = self.post("scheduled")
        self.tick(publisher=self.wrong)
        claim = self.wrong.store.claim_stuck

        def claimed_then_cancelled(*args, **kwargs):
            rows = claim(*args, **kwargs)
            self.service.cancel(self.user, post_id)
            return rows

        self.wrong.store.claim_stuck = claimed_then_cancelled
        self.tick(later=STUCK_AFTER + 1, publisher=self.wrong)
        self.assertEqual((self.status(post_id), self.counted("unconfirmed")), ("cancelled", 0))
