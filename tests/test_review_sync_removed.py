# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #230 (a file removed from the repository cancels its pending post): each case failed on the
build's head and passes with its fix."""

from inkcast.libs.platforms import PlatformError

from test_sync_error_after_post import at
from test_sync_removed import RemovedCase


class AFilePutBackAfterItsUncertainPostWasRemoved(RemovedCase):
    """A removal clears the entry's hash so a file put back is published anew. For a post that may already be on the
    platform ('unconfirmed', 'recovering'), that made a revert of the removal send a second copy, which a person's
    cancel never does: there, the same file stays 'unchanged'."""

    def test_an_unconfirmed_post_that_arrived_is_not_sent_again_when_the_file_is_put_back(self):
        self.platform.error = PlatformError("timed out after sending", uncertain=True)
        first = self.sync()  # its answer was lost, but it arrived: the platform has it
        self.platform.error = None
        self.assertEqual((first.status, self.platform.sent), ("unconfirmed", ["Body."]))
        self.assertEqual((self.remove().action, self.store.get_post(first.post_id)["status"]), ("removed", "cancelled"))
        again = self.sync()  # the removal is reverted: the same file is back
        self.assertEqual(self.platform.sent, ["Body."], "a second copy went to the platform")
        self.assertEqual((again.action, again.status, again.post_id), ("unchanged", "cancelled", first.post_id))

    def test_a_recovering_post_is_not_made_again_when_the_file_is_put_back(self):
        first = self.sync(at(2 * 24 * 3600))
        self.store.update_post(first.post_id, status="recovering")  # its send was interrupted: it may have arrived
        self.remove()
        again = self.sync(at(2 * 24 * 3600))
        self.assertEqual((again.action, again.post_id), ("unchanged", first.post_id))
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled"], "a second post was made")

    def test_a_scheduled_post_was_never_sent_and_is_made_again(self):
        first = self.sync(at(2 * 24 * 3600))
        self.remove()
        again = self.sync(at(2 * 24 * 3600))
        self.assertEqual((again.action, again.status), ("create", "scheduled"))
        self.assertNotEqual(again.post_id, first.post_id)
