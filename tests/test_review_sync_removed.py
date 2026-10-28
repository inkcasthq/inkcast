# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #230 (a file removed from the repository cancels its pending post): each case failed on the
build's head and passes with its fix."""

import threading
import unittest
from unittest import mock

from inkcast.cli import collect, removed_since
from inkcast.libs.platforms import PlatformError

from test_sync_error_after_post import at
from test_sync_removed import RemovedCase
from test_sync_removed_client import inside, repo_with_a_removal


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


class ARemovalWhileAnotherSyncHoldsTheFile(RemovedCase):
    """Another sync is making the file's post: its claim names the post before that sync's last write (the entry's
    status). A removal in between cancelled the post, and that last write then recorded it 'scheduled', for good: the
    file is gone, so no later sync follows the post. A fresh claim is another sync's work in progress, post or not."""

    def test_a_removal_between_the_claim_naming_its_post_and_the_last_write_waits(self):
        real, removal = self.store.put_entry, {}

        def last_write(*args, **kwargs):  # the creating sync's last write: the removal comes in just before it
            if kwargs.get("status") == "scheduled" and not removal:
                other = threading.Thread(target=lambda: removal.update(result=self.remove()))
                other.start()
                other.join()
            return real(*args, **kwargs)

        with mock.patch.object(self.store, "put_entry", last_write):
            made = self.sync(at(2 * 24 * 3600))
        self.assertIn("result", removal, "the removal never ran")
        post, entry = self.store.get_post(made.post_id), self.entry()
        self.assertEqual(entry["status"], post["status"], "the ledger names the post with a status it does not have")
        r = removal["result"]
        self.assertEqual((r.action, r.status), ("skipped", "syncing"))
        self.assertIn("run the sync again", r.message)
        rerun = self.remove()
        self.assertEqual((rerun.action, rerun.status, rerun.post_id), ("removed", "cancelled", made.post_id))
        self.assertEqual((self.entry()["status"], self.store.get_post(made.post_id)["status"]), ("cancelled", "cancelled"))


class TheCliReportsWhatItWouldHaveSynced(unittest.TestCase):
    def test_a_pattern_written_with_a_leading_dot_slash_reports_its_removed_files(self):
        """``collect`` drops a leading ``./`` (``posts/kept.md`` is what the ledger knows), so the same pattern must match
        git's paths, which never have one: otherwise every removal is missed and the post goes out."""
        repo, first = repo_with_a_removal()
        with inside(repo):
            self.assertEqual(collect(["./posts/*.md"]), ["posts/kept.md", "posts/moved.md"])
            self.assertEqual(removed_since(first, ["./posts/**/*.md"]), ["posts/a.md", "posts/renamed.md"])
            self.assertEqual(removed_since(first, ["./posts/a.md"]), ["posts/a.md"])
