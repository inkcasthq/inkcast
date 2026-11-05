# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #473 (the OB3 review of PR #456): the sync that finds a file unchanged settles the "changed while this send was
out" note only while the post holds the file.

``GitSync._one`` reads a file as unchanged when it hashes as its entry does, and then ``Publisher.settle_edited`` clears
the note (#427). The entry holds the hash of what the platform has only once it has followed the post into a confirmed
status (#392). A success landing on a row already confirmed with its copy (``PostsRepo.same_copy``) rewrites the
payload and adds the note without changing the status, so nothing follows, and the entry keeps the hash of content the
platform never received. One way there: a lookup found that copy for content it does not hold, matching on less than
the whole payload, as DEV's does (``DevtoPlatform.find_existing``
compares the title, the canonical URL, the body and the status, not the tags or the description); ``Editable`` here
compares the body only, so a new title is the edit it can't tell apart.

Driven as it happens: attempt 2 is out; a stuck pass is told "missing" and queues it again; the file's title is edited
in place; attempt 3's worker stops mid-send; attempt 2 arrives; the next stuck pass's lookup finds it for the edited
post and records it; a sync follows it; attempt 2's answer lands last. The next sync read the file unchanged and cleared
the note, though the platform has the old title: the change it still owed went unsaid. Failed on main. The case #427
settles, a file put back to what was sent, is pinned by ``test_sync_edited_note_427.FindingARevertClearsTheNote``."""

import asyncio
import time
import unittest

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.pipeline import STUCK_AFTER
from inkcast.libs.platforms import PublishResult

from test_sync_edit_during_send_392 import Editable
from test_sync_edited_note_427 import EditedWhileOut
from test_sync_error_after_post import PATH, SOURCE, at, markdown


class AnsweredLate(Editable):
    """Editable whose next send runs ``during_publish(prepared, arrive)`` while it is out: ``arrive()`` is the request
    reaching the platform, and the answer, naming the post it made, comes back once the hook returns."""

    async def publish(self, creds, account, prepared, destination):
        hook, self.during_publish = self.during_publish, None
        if hook is None:
            return await super().publish(creds, account, prepared, destination)
        made: list[int] = []

        def arrive():
            self.sent.append(prepared.payload["body"].strip())
            made.append(len(self.sent))

        await hook(prepared, arrive)
        (n,) = made
        return PublishResult(url=f"https://{self.id}/{n}", remote_id=str(n), status="published")


def retitled(platform: str, when) -> str:
    return markdown(platform, when).replace("title: Locked", "title: Retitled")


class ChangeStillOwed(EditedWhileOut):
    platform_class = AnsweredLate

    async def push_async(self, text: str, dry_run: bool = False):
        """One push of the file as ``text`` (the title edited too, which ``sync_async`` can't write)."""
        req = SyncRequest(source=SOURCE, dry_run=dry_run, files=[SyncFile(path=PATH, markdown=text)])
        (result,) = (await self.git.sync(self.user, req)).results
        return result

    def push(self, text: str, dry_run: bool = False):
        return asyncio.run(self.push_async(text, dry_run))

    def owed(self):
        """The sequence of the module's docstring, up to attempt 2's answer landing last: the row holds the title the
        platform has ("Locked"), with the note; the entry the edited title's hash. ``(when, post)``."""
        when = at(-600)
        post = self.waiting(when)
        self.platform.sent.clear()                                            # the rate-limited send never arrived

        async def while_attempt_2_is_out(prepared, arrive):
            now = time.time() + STUCK_AFTER + 1
            (stuck,) = self.store.claim_stuck(older_than=now - STUCK_AFTER, now=now)
            await self.publisher.recover(stuck, now)                          # missing: queued again, attempt 2
            self.assertEqual((await self.push_async(retitled(self.platform.id, when))).action, "update")   # in place
            (claimed,) = self.store.claim_due_posts(now + 1)                  # attempt 3; its worker stops mid-send
            self.assertEqual(claimed["attempts"], 3)
            arrive()                                                          # attempt 2 reaches the platform
            later = now + 1 + STUCK_AFTER + 1
            (stuck,) = self.store.claim_stuck(older_than=later - STUCK_AFTER, now=later)
            found = await self.publisher.recover(stuck, later)                # the body matches: attempt 2's post
            self.assertEqual((found.status, found.remote_id), ("published", "1"))
            self.assertEqual((await self.push_async(retitled(self.platform.id, when))).action, "unchanged")   # followed

        self.platform.during_publish = while_attempt_2_is_out
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING"):
            asyncio.run(self.publisher.run_due(now=post["run_at"] + 1))      # attempt 2's answer lands last
        row = self.store.get_post(post["id"])
        self.assertEqual((row["status"], row["remote_id"], row["title"]), ("published", "1", "Locked"),
                         "the row does not record what the platform has")
        self.assertIsNotNone(self.note(post["id"]))
        return when, post


class FindingAChangeStillOwedKeepsItsNote(ChangeStillOwed):
    def test_an_unchanged_file_the_platform_never_received_keeps_the_note(self):
        when, post = self.owed()
        synced = self.push(retitled(self.platform.id, when))
        self.assertTrue(self.platform.updated or self.note(post["id"]),
                        f"the {synced.action} sync cleared the note of a change it never sent")


if __name__ == "__main__":
    unittest.main()
