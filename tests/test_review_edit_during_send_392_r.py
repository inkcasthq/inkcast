# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #419 (issue #392): an edit made while a send is out.

The fix makes the next sync send the edit as an update of the copy the late success recorded. That update is
automatic, and ``Publisher.update`` replaced the post's error with the update's own notes: any note the success wrote
beside the edit's (#214's "a second copy may be on the platform") was erased by a sync nobody watches, before a person
read it. An edit while the send was out needs a stuck takeover and recovery's re-queue, which is exactly when a second
send goes out, so the two notes come together."""

import asyncio
import time
import unittest

from inkcast.libs.pipeline import STUCK_AFTER
from inkcast.libs.platforms import PlatformError

from test_sync_edit_during_send_392 import Editable, EditDuringSend
from test_sync_error_after_post import at


class FindingTheUpdateKeepsTheSecondCopyNote(EditDuringSend):
    platform_class = Editable

    def test_the_update_that_sends_the_edit_keeps_the_note_of_a_second_copy(self):
        when = at(-600)
        post = self.waiting(when)

        async def while_a_waits(prepared):                                    # inside A's publish (attempt 2), out
            self.platform.sent.clear()                                        # nothing has arrived: the lookup says so
            now = time.time() + STUCK_AFTER + 1
            (stuck,) = self.store.claim_stuck(older_than=now - STUCK_AFTER, now=now)
            await self.publisher.recover(stuck, now)                          # missing → queued again, attempt 2
            self.assertEqual((await self.sync_async(when, "Edited.")).action, "update")   # in place
            self.platform.error = PlatformError("Editable timed out.", uncertain=True)
            await self.publisher.run_due(now=now + 1)                         # B, attempt 3, with the edit: unconfirmed
            self.platform.error = None
            self.assertEqual(self.store.get_post(post["id"])["status"], "unconfirmed")

        self.platform.during_publish = while_a_waits
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING"):
            asyncio.run(self.publisher.run_due(now=post["run_at"] + 1))      # A's 2xx lands last
        now = self.store.get_post(post["id"])
        self.assertEqual(now["status"], "published")
        self.assertIn("a second copy may be on Editable", now["error"])        # #214's note: B may have arrived
        self.assertIn("changed while this send was out", now["error"])

        synced = self.sync(when, body="Edited.")
        self.assertEqual((synced.action, self.platform.updated), ("update", [(now["remote_id"], "Edited.")]))
        after = self.store.get_post(post["id"])
        self.assertIn("a second copy may be on Editable", after["error"] or "",
                      "the automatic update erased the note that the platform may have two copies")
        self.assertNotIn("changed while this send was out", after["error"] or "", "the settled edit is still noted")


if __name__ == "__main__":
    unittest.main()
