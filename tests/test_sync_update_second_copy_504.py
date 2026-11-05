# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #504: an update keeps every second-copy note the post holds, and replaces only the platform's own notes from an
earlier send.

``Publisher.update`` kept the notes beside the edited-while-out note (#392), and ``_lost`` alone (#234). Every other
note went with the update, among them the two other notes a send's success writes when the platform may have the post
twice (``PostsRepo.record_success``, #214): another worker's attempt that may have arrived too, and "may have this post
twice", naming both copies. An edit is no decision about a second copy: only a person clears that note (#234). The
``Finding`` classes failed on da780ddc (``error after: None``, as the issue measured); the ``Held`` classes pin what
holds around them: the platform's notes from an earlier send are replaced, the edited-while-out note is settled with
every other note kept (#392, #427), and ``_lost`` is kept (#234)."""

import asyncio
import unittest

import test_review_publishing_214 as r214
from inkcast.libs.pipeline import Publisher, _edited, _lost
from inkcast.libs.platforms import PublishResult

from test_review_473_ob3 import another_workers_late_success
from test_review_sync_427_456 import TWICE
from test_sync_edit_during_send_392 import Editable
from test_sync_error_after_post import SyncCase

# The note a send's success leaves when another worker's attempt 2 was out meanwhile (``record_success``, #214).
ATTEMPT_2 = ("Attempt 2 was sent by another worker while this one was out, and may have arrived too: a second copy may "
             "be on Editable. Check Editable, and remove the extra copy there if it is.")
TRIMMED = "Editable trimmed the tags to four."                    # a platform's own note on a send


class Noting(Editable):
    """Editable whose update says something of its own, as a platform's notes do (DEV trims the tags)."""

    async def update(self, creds, account, remote_id, prepared, destination):
        result = await super().update(creds, account, remote_id, prepared, destination)
        return PublishResult(url=result.url, remote_id=result.remote_id, status=result.status,
                             notes=["Editable trimmed the tags of the update."])


class UpdateCase(SyncCase):
    platform_class = Noting

    def published_with(self, *notes: str) -> str:
        """The file synced and published (copy 1), then its post's error set to ``notes``, as the writers leave them."""
        first = self.sync()
        self.assertEqual(first.status, "published")
        self.store.update_post(first.post_id, error="; ".join(notes))
        return first.post_id

    def edited(self, post_id: str) -> dict:
        """An edit of the file, sent as an update; the post after it."""
        synced = self.sync(body="Edited.")
        after = self.store.get_post(post_id)
        self.assertEqual((synced.action, after["status"], self.platform.updated),
                         ("update", "published", [("1", "Edited.")]))
        return after


# ---- an update keeps every second-copy note --------------------------------------------------------------------------


class FindingANoteNamingTwoCopiesOutlivesTheUpdate(SyncCase):
    platform_class = Editable

    def test_a_row_with_a_may_have_this_post_twice_note_keeps_it_through_an_update(self):
        """The issue's measurement: a published post, another worker's success naming another copy (id 9), then an
        edited sync. ``error after: None`` on da780ddc."""
        first = self.sync()
        written, _ = another_workers_late_success(self)(first.post_id)
        self.assertEqual((written, self.store.get_post(first.post_id)["error"]), (False, TWICE))
        synced = self.sync(body="Edited.")
        after = self.store.get_post(first.post_id)
        self.assertEqual((synced.action, after["status"], self.platform.updated), ("update", "published", [("1", "Edited.")]))
        self.assertEqual(after["error"], TWICE, "the update dropped the note that the platform may have the post twice")


class FindingANoteThatAnotherAttemptMayHaveArrivedOutlivesTheUpdate(UpdateCase):
    def test_the_note_is_kept_and_the_updates_own_note_follows_it(self):
        post_id = self.published_with(ATTEMPT_2)
        self.assertEqual(self.edited(post_id)["error"], f"{ATTEMPT_2}; Editable trimmed the tags of the update.")


class FindingEverySecondCopyNoteOutlivesTheUpdateAndThePlatformsNoteIsReplaced(UpdateCase):
    def test_the_second_copy_notes_are_kept_in_order_and_the_earlier_sends_note_is_replaced(self):
        """As ``record_success`` words a success's error (the platform's notes, then ``_lost``, then another attempt's),
        with a note naming both copies added after: the update replaces the platform's note with its own, and keeps the
        three others as they were."""
        lost = _lost(self.platform)
        post_id = self.published_with(TRIMMED, lost, ATTEMPT_2, TWICE)
        self.assertEqual(self.edited(post_id)["error"],
                         f"{lost}; {ATTEMPT_2}; {TWICE}; Editable trimmed the tags of the update.")


class NotingFindable(r214.Findable):
    """Findable that edits posts in place, and says something of its own on an update."""

    capabilities = frozenset({"find", "update"})

    async def update(self, creds, account, remote_id, prepared, destination):
        return PublishResult(url=f"https://findable/{remote_id}", remote_id=remote_id, status="published",
                             notes=["Findable trimmed the tags of the update."])


class FindingTheNotesASendsSuccessWritesOutliveTheUpdate(r214.Harness):
    """The notes worded by the writers themselves, not copied into the test: worker A's success lands while worker B's
    re-send is out (attempt 2 may have arrived too), then B's success names another copy (both copies). An update of
    the post then keeps both (#504)."""

    a_success_lands_while_b_re_sends = r214.LateSuccessIsTheTruth.a_success_lands_while_b_re_sends

    def setUp(self):
        super().setUp()
        self.platform = NotingFindable()
        self.platforms = {self.platform.id: self.platform}
        self.publisher = Publisher(self.store, self.platforms)

    def test_an_update_keeps_both_notes(self):
        post_id, _, logged = self.a_success_lands_while_b_re_sends()
        self.assertEqual(len(logged), 2, "A's override and B's refused copy are both in the log")
        before = self.store.get_post(post_id)["error"]
        self.assertIn("Attempt 2 was sent by another worker", before)
        self.assertIn("Findable may have this post twice", before)
        outcome = asyncio.run(self.publisher.update(self.user, post_id, self.platform,
                                                    self.platform.prepare({"title": "t"}, "Edited."), "1"))
        after = self.store.get_post(post_id)
        self.assertEqual((outcome.status, after["status"], after["remote_id"]), ("published", "published", "1"))
        self.assertEqual(after["error"], f"{before}; Findable trimmed the tags of the update.")


# ---- what holds ----------------------------------------------------------------------------------------------------


class HeldThePlatformsNotesOfAnEarlierSendAreReplaced(UpdateCase):
    def test_a_platform_note_alone_is_replaced(self):
        post_id = self.published_with(TRIMMED)
        self.assertEqual(self.edited(post_id)["error"], "Editable trimmed the tags of the update.")

    def test_a_platform_note_worded_like_a_second_copy_note_but_not_one_is_replaced(self):
        """Recognised by the whole wording a writer gives it, never by a phrase in it: a platform's note that names a
        second copy, or a second-copy note cut short, is the platform's."""
        notes = ("A second copy may be on Editable.", TWICE.rsplit(" Check ", 1)[0], ATTEMPT_2.replace("Editable", "DEV"))
        post_id = self.published_with(*notes)
        self.assertEqual(self.edited(post_id)["error"], "Editable trimmed the tags of the update.")


class HeldTheLostNoteIsKept(UpdateCase):
    def test_the_lost_note_is_kept_and_the_platforms_note_beside_it_replaced(self):
        lost = _lost(self.platform)
        post_id = self.published_with(TRIMMED, lost)
        self.assertEqual(self.edited(post_id)["error"], f"{lost}; Editable trimmed the tags of the update.")


class HeldTheEditedNoteIsSettledAndEveryOtherNoteKept(UpdateCase):
    def test_the_update_settles_the_edited_note_and_keeps_the_rest_as_before(self):
        """#392's rule, unchanged: on a post that holds the edited-while-out note, the update that sends the edit
        settles that note only. The platform's note of the send that wrote it stays beside the second-copy notes."""
        first = self.sync()
        row = self.store.get_post(first.post_id)
        edited = _edited(self.platform, row["result_url"], row["remote_id"])
        self.store.update_post(first.post_id, error="; ".join((TRIMMED, ATTEMPT_2, edited, TWICE)))
        self.assertEqual(self.edited(first.post_id)["error"],
                         f"{TRIMMED}; {ATTEMPT_2}; {TWICE}; Editable trimmed the tags of the update.")


if __name__ == "__main__":
    unittest.main()
