# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #503: a change still owed after a lookup's match is sent by the next sync.

#473 kept the note "The post was changed while this send was out ... The next sync sends the change as an update" while
the post's own payload doesn't hold the file (``Publisher.settle_edited``'s ``holds``). Nothing kept its promise: the
file's entry has the file's hash, so every later sync answered ``unchanged``. ``GitSync._one`` now also reads a confirmed
post whose stored payload doesn't hash as the file as changed: the update is sent, and its success settles the note.

- ``FindingAChangeStillOwedIsSent``: the sequence of ``test_sync_settle_owed_473`` (a lookup that matches the body only
  records attempt 2's copy for the retitled post; attempt 2's answer, the old title, lands last). The next sync sends the
  title as one update, the note clears, and the sync after is ``unchanged``.
- ``FindingAChangeStillOwedIsReportedStale``: the same sequence where the platform doesn't edit posts: the sync says the
  post must be updated by hand, as for any change there, and sends nothing.
- ``FindingAnUpdateOvertakenByItsCopysAnswerIsSentAgain``: the issue's concern 2. A lookup finds attempt 2's copy, the
  sync PUTs the edit to it, and attempt 2's own answer lands last: the row holds what that send carried while the
  platform has the edit. The note it leaves promised an update the platform already had, and stayed. Now the next sync
  sends the edit again (a PUT of the same content to the same copy), which makes the note true and settles it.

Each ``Finding`` failed on 81009c4f. What holds around them (no PUT for a file whose post holds it) is pinned by
``test_sync_edit_during_send_392`` (``FindingTheEditIsSentAsAnUpdate``, ``HeldASuccessForTheContentThePostHolds``) and
``test_sync_edited_note_427`` (``FindingARevertClearsTheNote``, ``HeldAnUpdateStillSettlesTheNote``)."""

import asyncio
import time
import unittest

from inkcast.libs.content.story import parse_story
from inkcast.libs.pipeline import STUCK_AFTER
from inkcast.services.publishing.sync import payload_hash

from test_sync_error_after_post import at
from test_sync_settle_owed_473 import AnsweredLate, ChangeStillOwed, retitled


class Titled(AnsweredLate):
    """AnsweredLate that records the title each update carried: the change owed in #473's sequence is the title."""

    def __init__(self):
        super().__init__()
        self.put: list[tuple[str, str]] = []

    async def update(self, creds, account, remote_id, prepared, destination):
        self.put.append((remote_id, prepared.title))
        return await super().update(creds, account, remote_id, prepared, destination)


class ByHand(AnsweredLate):
    """AnsweredLate on a platform that can be asked but doesn't let Inkcast edit posts."""

    id, name, default_api_url = "byhand", "ByHand", "https://byhand.invalid"
    capabilities = frozenset({"find"})


class FindingAChangeStillOwedIsSent(ChangeStillOwed):
    platform_class = Titled

    def test_the_next_sync_sends_the_change_and_settles_the_note(self):
        when, post = self.owed()
        self.assertEqual(self.entry()["content_hash"], self.digest_of(retitled(self.platform.id, when)),
                         "the entry does not hash as the file: not the case of #503")
        held = self.store.notes_of(self.store.get_post(post["id"]))
        others = [n for n in held if n != self.note(post["id"])]
        self.assertEqual(len(others), 1, held)
        self.assertIn("Attempt 3 was sent by another worker", others[0])     # a second copy, the person's to check

        dry = self.push(retitled(self.platform.id, when), dry_run=True)
        self.assertEqual((dry.action, dry.post_id), ("update", post["id"]), "a dry run says the change is not sent")
        self.assertEqual(self.platform.put, [])
        self.assertIsNotNone(self.note(post["id"]), "a dry run cleared the note")

        synced = self.push(retitled(self.platform.id, when))
        self.assertEqual((synced.action, synced.status, synced.post_id), ("update", "published", post["id"]),
                         "the change the note promised was never sent")
        self.assertEqual(self.platform.put, [("1", "Retitled")])
        row = self.store.get_post(post["id"])
        self.assertEqual((row["title"], self.note(post["id"])), ("Retitled", None), "the note outlived the update")
        self.assertEqual(self.store.notes_of(row), others, "a note only a person clears went with it")
        self.assertEqual(self.entry()["content_hash"], self.digest_of(retitled(self.platform.id, when)))

        again = self.push(retitled(self.platform.id, when))
        self.assertEqual((again.action, again.status), ("unchanged", "published"))
        self.assertEqual(self.platform.put, [("1", "Retitled")], "sent again")

    def digest_of(self, text: str) -> str:
        """The hash the sync gives the file ``text``."""
        story = parse_story(text, known_platforms=[self.platform.id], schedule_as_written=True)
        return payload_hash(self.platform.prepare(story.for_platform(self.platform.id), story.body))


class FindingAChangeStillOwedIsReportedStale(ChangeStillOwed):
    platform_class = ByHand

    def test_the_next_sync_names_the_copy_to_update_by_hand(self):
        when, post = self.owed()
        row, note = self.store.get_post(post["id"]), self.note(post["id"])
        self.assertIn(f"update it by hand: {row['result_url']}", note)

        synced = self.push(retitled(self.platform.id, when))
        self.assertEqual((synced.action, synced.status, synced.url), ("stale", "published", row["result_url"]),
                         "a change the platform never received was reported unchanged")
        self.assertIn(f"Update it by hand: {row['result_url']}", synced.message)
        self.assertEqual(self.platform.sent, ["Body."], "posted again")
        self.assertEqual(self.note(post["id"]), note, "the note changed under a stale result")
        self.assertEqual(self.push(retitled(self.platform.id, when)).action, "stale")


class FindingAnUpdateOvertakenByItsCopysAnswerIsSentAgain(ChangeStillOwed):
    def test_the_next_sync_sends_the_edit_again_and_settles_the_note(self):
        when = at(-600)
        post = self.waiting(when)
        self.platform.sent.clear()                                            # the rate-limited send never arrived

        async def while_attempt_2_is_out(prepared, arrive):
            arrive()                                                          # attempt 2 reaches the platform: copy 1
            now = time.time() + STUCK_AFTER + 1
            (stuck,) = self.store.claim_stuck(older_than=now - STUCK_AFTER, now=now)
            found = await self.publisher.recover(stuck, now)                  # the lookup finds copy 1
            self.assertEqual((found.status, found.remote_id), ("published", "1"))
            edited = await self.sync_async(when, "Edited.")                  # followed, then the edit PUT to copy 1
            self.assertEqual((edited.action, self.platform.updated), ("update", [("1", "Edited.")]))

        self.platform.during_publish = while_attempt_2_is_out
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING"):
            asyncio.run(self.publisher.run_due(now=post["run_at"] + 1))      # attempt 2's answer lands last
        row = self.store.get_post(post["id"])
        self.assertEqual((row["status"], row["remote_id"], self.body_of(row)), ("published", "1", "Body."))
        self.assertIn("The next sync sends the change as an update", self.note(post["id"]))
        self.assertEqual(self.entry()["content_hash"], self.digest("Edited."))

        synced = self.sync(when, body="Edited.")
        self.assertEqual((synced.action, synced.post_id), ("update", post["id"]),
                         "the row says the platform has what attempt 2 carried, and the note stayed")
        self.assertEqual(self.platform.updated, [("1", "Edited."), ("1", "Edited.")])
        row = self.store.get_post(post["id"])
        self.assertEqual((self.body_of(row), row["error"]), ("Edited.", None))
        self.assertEqual(self.sync(when, body="Edited.").action, "unchanged")
        self.assertEqual(len(self.platform.updated), 2, "sent again")


if __name__ == "__main__":
    unittest.main()
