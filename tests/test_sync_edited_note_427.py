# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #427: the note a send's success leaves on a post changed while it was out says what happens to the change.

Since #392 (PR #419) that note promised "The next sync sends the change as an update" (or named the copy to update by
hand). The OB3 review of PR #419 measured three cases where nothing kept the promise, each driven here as it was there
(``EditedWhileOut.success_lands_after``: the re-queue, then the hook's edits, cancels and syncs, then the success):

1. the person cancelled the post and a sync followed it into 'cancelled' before the success landed: the entry was never
   followed out of 'cancelled', so the next sync read ``unchanged`` and the note stayed
   (``FindingACancelledPostThatWentOutIsFollowed``, ``...IsReportedStale``);
2. the file was put back to what was sent: the sync read ``unchanged``, as it should, and the note stayed
   (``FindingARevertClearsTheNote``);
3. the success named the post's URL but no id: the note promised an update ``GitSync._one`` can't make without a
   ``remote_id`` (``FindingASuccessWithoutAnIdSaysByHand``).

Each ``Finding`` failed on main; the ``Held`` classes pin what holds around them."""

import asyncio
import time
import unittest

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.pipeline import STUCK_AFTER, _lost
from inkcast.libs.platforms import PublishResult

from test_sync_edit_during_send_392 import Editable, EditDuringSend, Hooked
from test_sync_error_after_post import PATH, SOURCE, at, markdown


class NoId(Editable):
    """Editable whose success names the post's URL but no id: a 2xx whose body names only where the post is."""

    id, name, default_api_url = "noid", "NoId", "https://noid.invalid"

    async def publish(self, creds, account, prepared, destination):
        r = await super().publish(creds, account, prepared, destination)
        return PublishResult(url=r.url, remote_id=None, status=r.status)


class EditedWhileOut(EditDuringSend):
    platform_class = Editable

    def success_lands_after(self, inside):
        """A's retry goes out and is out past STUCK_AFTER: B's stuck pass is told "missing" and queues the post again at
        the same attempt count; then ``inside(when, post)`` runs (edits, cancels, syncs); then A's success lands and is
        recorded as the platform's truth, with the edited note (#392). ``(when, post)``."""
        when = at(-600)
        post = self.waiting(when)

        async def while_a_waits(prepared):
            self.platform.sent.clear()
            now = time.time() + STUCK_AFTER + 1
            (stuck,) = self.store.claim_stuck(older_than=now - STUCK_AFTER, now=now)
            await self.publisher.recover(stuck, now)
            await inside(when, post)

        self.platform.during_publish = while_a_waits
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING"):
            asyncio.run(self.publisher.run_due(now=post["run_at"] + 1))
        return when, post

    def note(self, post_id: str) -> str | None:
        """The edited-while-out note on the post, whatever it says the change's fate is."""
        row = self.store.get_post(post_id)
        return next((n for n in self.store.notes_of(row) if "changed while this send was out" in n), None)

    def dry_run(self, when, body: str):
        req = SyncRequest(source=SOURCE, dry_run=True, files=[SyncFile(path=PATH, markdown=markdown(self.platform.id, when, body))])
        (result,) = asyncio.run(self.git.sync(self.user, req)).results
        return result


async def edit_then_cancel_then_sync(case: EditedWhileOut, when, post):
    """The person edits the file (in place: the post waits for its retry), cancels the post, and a push follows it into
    'cancelled', all while the send is still out."""
    await case.sync_async(when, "Edited.")
    case.publisher.cancel(case.user, post["id"])
    await case.sync_async(when, "Edited.")


# ---- 1. a cancelled post that went out after all ------------------------------------------------------------------


class FindingACancelledPostThatWentOutIsFollowed(EditedWhileOut):
    def test_the_next_sync_sends_the_change_as_the_note_says(self):
        when, post = self.success_lands_after(lambda when, post: edit_then_cancel_then_sync(self, when, post))
        row = self.store.get_post(post["id"])
        self.assertEqual((row["status"], self.body_of(row), self.entry()["status"]), ("published", "Body.", "cancelled"))
        self.assertIn("The next sync sends the change as an update", row["error"])

        synced = self.sync(when, body="Edited.")
        self.assertEqual((synced.action, synced.status, synced.post_id), ("update", "published", post["id"]),
                         "the note promised an update that never came")
        self.assertEqual(self.platform.updated, [("1", "Edited.")])
        entry = self.entry()
        self.assertEqual((entry["status"], entry["content_hash"], entry["remote_id"]), ("published", self.digest("Edited."), "1"))
        self.assertIsNone(self.store.get_post(post["id"])["error"], "the note outlived the update")
        self.assertEqual(self.sync(when, body="Edited.").action, "unchanged")
        self.assertEqual(self.platform.updated, [("1", "Edited.")], "sent again")


class FindingACancelledPostThatWentOutIsReportedStale(EditedWhileOut):
    platform_class = Hooked

    def test_the_next_sync_names_the_copy_to_update_by_hand(self):
        when, post = self.success_lands_after(lambda when, post: edit_then_cancel_then_sync(self, when, post))
        row = self.store.get_post(post["id"])
        self.assertEqual((row["status"], self.entry()["status"]), ("published", "cancelled"))
        self.assertIn(f"update it by hand: {row['result_url']}", row["error"])

        synced = self.sync(when, body="Edited.")
        self.assertEqual((synced.action, synced.status, synced.url), ("stale", "published", row["result_url"]),
                         "the edit was reported unchanged")
        self.assertIn(f"Update it by hand: {row['result_url']}", synced.message)
        self.assertEqual((self.platform.sent, self.entry()["status"]), (["Body."], "published"))


class HeldACancelledPostThatNeverWentOutIsNotFollowed(EditedWhileOut):
    def test_an_edit_after_a_cancel_is_a_new_post(self):
        """A cancel that took effect: the post is 'cancelled', and an edited file is published as a new post, as the
        docs say. Only a post the platform confirms is followed out of 'cancelled'."""
        when = at(-600)
        post = self.waiting(when)
        self.publisher.cancel(self.user, post["id"])
        synced = self.sync(when, body="Edited.")
        self.assertEqual(synced.action, "create")                            # a new post
        self.assertNotEqual(synced.post_id, post["id"])
        self.assertEqual((self.store.get_post(post["id"])["status"], self.platform.updated), ("cancelled", []))


# ---- 2. the file put back to what was sent ------------------------------------------------------------------------


class FindingARevertClearsTheNote(EditedWhileOut):
    def test_a_sync_that_finds_the_file_as_it_was_sent_clears_the_note_and_keeps_the_others(self):
        when, post = self.success_lands_after(lambda when, post: self.sync_async(when, "Edited."))
        self.assertIsNotNone(self.note(post["id"]))
        lost = _lost(self.platform)                                           # a note only a person clears (#234)
        self.store.update_post(post["id"], error=f"{lost}; {self.note(post['id'])}")

        self.assertEqual(self.dry_run(when, "Body.").action, "unchanged")
        self.assertIsNotNone(self.note(post["id"]), "a dry run cleared the note")
        reverted = self.sync(when, body="Body.")
        self.assertEqual((reverted.action, reverted.status), ("unchanged", "published"))
        self.assertIsNone(self.note(post["id"]), "the note still promises an update nothing owes")
        self.assertEqual(self.store.get_post(post["id"])["error"], lost, "another note was cleared with it")
        self.assertEqual(self.platform.updated, [])


class FindingARevertClearsTheByHandNoteToo(EditedWhileOut):
    platform_class = Hooked

    def test_on_a_platform_that_does_not_edit_posts(self):
        when, post = self.success_lands_after(lambda when, post: self.sync_async(when, "Edited."))
        self.assertIn("update it by hand", self.note(post["id"]))
        reverted = self.sync(when, body="Body.")
        self.assertEqual((reverted.action, self.note(post["id"]), self.store.get_post(post["id"])["error"]),
                         ("unchanged", None, None))


# ---- 3. a success that names a URL but no id ----------------------------------------------------------------------


class FindingASuccessWithoutAnIdSaysByHand(EditedWhileOut):
    platform_class = NoId

    def test_the_note_and_the_next_sync_say_the_post_must_be_updated_by_hand(self):
        when, post = self.success_lands_after(lambda when, post: self.sync_async(when, "Edited."))
        row = self.store.get_post(post["id"])
        self.assertEqual((row["status"], row["remote_id"], self.body_of(row)), ("published", None, "Body."))
        note = self.note(post["id"])
        self.assertNotIn("The next sync sends the change", note, "an update nothing can make was promised")
        self.assertIn(f"update it by hand: {row['result_url']}", note)
        self.assertIn("named no id", note)

        synced = self.sync(when, body="Edited.")
        self.assertEqual((synced.action, synced.status, synced.url), ("stale", "published", row["result_url"]))
        self.assertIn("named no id", synced.message)
        self.assertIn(f"Update it by hand: {row['result_url']}", synced.message)
        self.assertEqual(self.platform.updated, [], "an update was sent without an id")
        self.assertEqual(self.note(post["id"]), note, "the note changed under a stale result")


class HeldAnUpdateStillSettlesTheNote(EditedWhileOut):
    def test_the_update_that_sends_the_edit_clears_the_note_as_before(self):
        when, post = self.success_lands_after(lambda when, post: self.sync_async(when, "Edited."))
        synced = self.sync(when, body="Edited.")
        self.assertEqual((synced.action, self.platform.updated), ("update", [("1", "Edited.")]))
        self.assertEqual((self.note(post["id"]), self.store.get_post(post["id"])["error"]), (None, None))


if __name__ == "__main__":
    unittest.main()
