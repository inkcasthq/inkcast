# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of 12e5c835 (issue #559). Each ``Finding`` class failed on 12e5c835.

1. ``GitSync._create`` wrote the hash of what its own send carried over an entry whose post records another worker's copy
   and other content. The file then read as changed at the next sync, though the platform had it: one more update where
   the platform edits posts, and "update it by hand" at every sync where it doesn't.
2. ``render_resend`` shows a confirmed post's status and URL only. A resend's send whose uncertain answer lands on another
   worker's copy now reads 'published', so the MCP reply lost the note it showed while that post read 'unconfirmed'."""

import asyncio
import json
import time
import unittest

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db.ledger import CLAIM_TTL
from inkcast.libs.pipeline import STUCK_AFTER, _lost
from inkcast.libs.platforms import PlatformError
from inkcast.services.publishing.sync import stored_hash
from inkcast.services.publishing.mcp_tools import render_resend
from test_person_decides import SyncHarness
from test_sync_edit_during_send_392 import Editable, Hooked
from test_sync_error_after_post import PATH, SOURCE, SyncCase, markdown


def later() -> float:
    return time.time() + STUCK_AFTER + 1


class OverACopyWithOtherContent(SyncCase):
    """Sync 1 sends the file as X, and its send is out past STUCK_AFTER and CLAIM_TTL. Worker B re-sends the post, and
    sync 2 carries the file as Y to it. Then sync 1's answer lands, and its outcome reaches the ledger."""

    def file(self, body: str) -> SyncRequest:
        return SyncRequest(source=SOURCE, files=[SyncFile(path=PATH, markdown=markdown(self.platform.id, None, body))])

    async def second_sync(self) -> None:
        self.store._exec("UPDATE ledger SET synced_at = synced_at - ?", CLAIM_TTL + 1)
        await self.git.sync(self.user, self.file("Y."))

    def assert_the_entry_hashes_what_the_post_holds(self, post_id: str) -> None:
        post = self.store.get_post(post_id)
        self.assertEqual(post["status"], "published")
        self.assertEqual(self.entry()["content_hash"], stored_hash(json.loads(post["payload"])))
        self.assertEqual((self.entry()["post_id"], self.entry()["url"], self.entry()["remote_id"]),
                         (post_id, post["result_url"], post["remote_id"]))


class FindingAnUncertainAnswerKeepsTheEntryOnWhatThePostHolds(OverACopyWithOtherContent):
    platform_class = Editable

    def test_the_file_updated_while_the_send_was_out_is_not_sent_again(self):
        async def out(prepared):
            await self.publisher.run_due(now=later())                 # B sends X: copy 1
            await self.second_sync()                                  # an update: the post holds Y, copy 1 has it
            raise PlatformError("Editable timed out after the request was sent.", uncertain=True)

        self.platform.during_publish = out
        first = self.sync(body="X.")
        self.assertEqual((first.status, first.message), ("published", _lost(self.platform)))
        self.assert_the_entry_hashes_what_the_post_holds(first.post_id)
        again = self.sync(body="Y.")
        self.assertEqual((again.action, self.platform.updated), ("unchanged", [("1", "Y.")]), "the same update sent twice")

    def test_a_late_success_refused_over_the_copy_likewise(self):
        """The same write, from a success refused over the copy (``record_success``), which was wrong on f5869458 too."""
        async def out(prepared):
            await self.publisher.run_due(now=later())
            await self.second_sync()

        self.platform.during_publish = out
        first = self.sync(body="X.")
        self.assertEqual((first.status, first.url), ("published", "https://editable/1"))
        self.assert_the_entry_hashes_what_the_post_holds(first.post_id)
        self.assertEqual(self.sync(body="Y.").action, "unchanged")


class HeldTheFileIsStillComparedWithWhatThePlatformHas(OverACopyWithOtherContent):
    platform_class = Editable

    def test_the_file_back_to_what_the_lost_send_carried_is_still_sent(self):
        """The hash is the post's, so what the file asks for is still compared with what the platform has."""
        async def out(prepared):
            await self.publisher.run_due(now=later())
            await self.second_sync()
            raise PlatformError("Editable timed out after the request was sent.", uncertain=True)

        self.platform.during_publish = out
        self.sync(body="X.")
        again = self.sync(body="X.")
        self.assertEqual((again.action, self.platform.updated), ("update", [("1", "Y."), ("1", "X.")]))


class FindingAnUncertainAnswerIsNotReportedStale(OverACopyWithOtherContent):
    platform_class = Hooked

    def test_a_file_edited_in_place_and_sent_by_the_re_send_is_not_stale(self):
        """No update on this platform: "update it by hand" at every sync, for a change the platform has."""
        async def out(prepared):
            self.platform.error = PlatformError("Findable is rate limited.", retryable=True)
            await self.publisher.run_due(now=later())                 # B's 429: waits for its retry, attempts 1
            self.platform.error = None
            await self.second_sync()                                  # edited in place: the post holds Y
            await self.publisher.run_due(now=later() + 7200)          # B's retry sends Y: published
            raise PlatformError("Findable timed out after the request was sent.", uncertain=True)

        self.platform.during_publish = out
        first = self.sync(body="X.")
        self.assert_the_entry_hashes_what_the_post_holds(first.post_id)
        again = self.sync(body="Y.")
        self.assertEqual((again.action, again.status, again.message), ("unchanged", "published", None))
        self.assertEqual(self.entry()["message"], None)


class FindingTheMCPResendReplyShowsTheNote(SyncHarness):
    def test_an_uncertain_answer_over_a_re_sends_copy(self):
        old = self.unconfirmed()
        other = self.replica()

        async def out(prepared):
            await other.run_due(now=later())
            raise PlatformError("Findable timed out after the request was sent.", uncertain=True)

        self.platform.during_publish = out
        reply = asyncio.run(self.service.resend(self.user, old))
        self.assertEqual((reply.status, reply.error), ("published", _lost(self.platform)))
        text = render_resend(reply, old, self.platforms)
        self.assertIn(f"published → {reply.url}\n  {_lost(self.platform)}\n  The first post is cancelled", text)

    def test_a_confirmed_post_with_no_note_reads_as_it_did(self):
        reply = render_resend(self.service._post({**self.store.get_post(self.post("published")), "result_url": "https://f/1"}),
                              "pst_old", self.platforms)
        self.assertIn("published → https://f/1\n  The first post is cancelled;", reply)


if __name__ == "__main__":
    unittest.main()
