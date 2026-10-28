# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #230: a file removed from the repository cancels its pending post.

``SyncRequest.removed`` lists the paths a push deleted. For each, the sync cancels the file's pending post ('scheduled',
'recovering' or 'unconfirmed') through the transition a person's cancel uses, with the ledger entry's write in the
cancel's transaction, and leaves a published post on the platform. A removal reads only the caller's entries of that
repository, so another person's post, and a post of the same path from another repository, are never touched. A dry
run changes nothing and says what would happen. The request needs ``files`` or ``removed``, and a path can't be in both.

Two syncs of the same path at once (one removing the file, one replacing its post with an edit) are raced at the
cancel itself, which is one conditional write: the competitor runs just before it."""

import asyncio
import json
import threading
import unittest
from unittest import mock

from pydantic import ValidationError

from inkcast.libs.contracts.models import SyncFile, SyncRequest, SyncResult

from test_sync_error_after_post import PATH, SOURCE, SyncCase, at, markdown


class RemovedCase(SyncCase):
    def remove(self, path: str = PATH, dry_run: bool = False, source: str = SOURCE) -> SyncResult:
        (result,) = asyncio.run(self.git.sync(self.user, SyncRequest(source=source, removed=[path], dry_run=dry_run))).results
        return result

    def sync_as(self, user_id: str, source: str = SOURCE) -> SyncResult:
        req = SyncRequest(source=source, files=[SyncFile(path=PATH, markdown=markdown(self.platform.id, at(2 * 24 * 3600)))])
        (result,) = asyncio.run(self.git.sync(user_id, req)).results
        return result

    def events(self, kind: str) -> list[dict]:
        return [json.loads(r["attrs"]) for r in self.store._all("SELECT attrs FROM outbox WHERE type = ? ORDER BY seq", kind)]


class ARemovedFilesPendingPostIsCancelled(RemovedCase):
    def test_a_scheduled_post_is_cancelled_and_never_sent(self):
        first = self.sync(at(2 * 24 * 3600))
        self.assertEqual(first.status, "scheduled")
        r = self.remove()
        self.assertEqual((r.action, r.status, r.post_id), ("removed", "cancelled", first.post_id))
        self.assertEqual(r.message, f"Post {first.post_id} was cancelled.")
        self.assertEqual(self.store.get_post(first.post_id)["status"], "cancelled")
        entry = self.entry()
        self.assertEqual((entry["status"], entry["post_id"], entry["content_hash"]), ("cancelled", first.post_id, ""))
        self.assertIn("Removed from the repository", entry["message"])
        asyncio.run(self.publisher.run_due(now=at(3 * 24 * 3600).timestamp()))  # its time passes: nothing goes out
        self.assertEqual(self.platform.sent, [])
        self.assertEqual([e["action"] for e in self.events("sync.result")], ["create", "removed"])
        self.assertEqual(len(self.events("post.cancelled")), 1)

    def test_a_recovering_post_is_cancelled_and_may_be_on_the_platform(self):
        first = self.sync(at(2 * 24 * 3600))
        self.store.update_post(first.post_id, status="recovering")
        r = self.remove()
        self.assertEqual((r.action, r.status, r.post_id), ("removed", "cancelled", first.post_id))
        self.assertEqual(r.message, f"Post {first.post_id} was cancelled. It may already be on Findable: check, and "
                                    "remove it there if it is.")
        post = self.store.get_post(first.post_id)
        self.assertEqual(post["status"], "cancelled")
        self.assertIn("may already be on Findable", post["error"])  # the note a person's cancel leaves (#216)

    def test_an_unconfirmed_post_is_cancelled_and_may_be_on_the_platform(self):
        first = self.sync(at(2 * 24 * 3600))
        self.store.update_post(first.post_id, status="unconfirmed")
        r = self.remove()
        self.assertEqual((r.action, r.status, r.post_id), ("removed", "cancelled", first.post_id))
        self.assertIn("It may already be on Findable", r.message)
        post = self.store.get_post(first.post_id)
        self.assertEqual(post["status"], "cancelled")
        self.assertIn("may already be on Findable", post["error"])  # the note a person's cancel leaves (#226)

    def test_a_published_post_is_left_alone(self):
        first = self.sync()
        self.assertEqual((first.status, first.url), ("published", "https://findable/1"))
        r = self.remove()
        self.assertEqual((r.action, r.status, r.post_id, r.url), ("removed", "published", first.post_id, first.url))
        self.assertIn("stays on Findable", r.message)
        self.assertEqual(self.store.get_post(first.post_id)["status"], "published")
        entry = self.entry()
        self.assertEqual((entry["status"], entry["post_id"]), ("published", first.post_id))
        self.assertNotEqual(entry["content_hash"], "")
        self.assertEqual(self.events("post.cancelled"), [])

    def test_a_path_with_no_entry_changes_nothing(self):
        r = self.remove("posts/never-synced.md")
        self.assertEqual((r.action, r.platform, r.status, r.post_id), ("removed", "*", None, None))
        self.assertEqual(r.message, "No post came from this file; nothing to cancel.")
        self.assertEqual((self.posts(), self.store.list_entries(self.user)), ([], []))

    def test_a_dry_run_changes_nothing_and_says_what_would_happen(self):
        first = self.sync(at(2 * 24 * 3600))
        r = self.remove(dry_run=True)
        self.assertEqual((r.action, r.status, r.post_id), ("removed", "scheduled", first.post_id))
        self.assertEqual(r.message, f"Would cancel post {first.post_id} (scheduled).")
        self.assertEqual(self.store.get_post(first.post_id)["status"], "scheduled")
        entry = self.entry()
        self.assertEqual((entry["status"], entry["post_id"], entry["message"]), ("scheduled", first.post_id, None))
        self.assertNotEqual(entry["content_hash"], "")
        self.assertEqual(self.events("post.cancelled"), [])
        self.assertEqual([e["action"] for e in self.events("sync.result")], ["create"])  # a dry run writes no event

    def test_a_file_put_back_is_published_as_a_new_post(self):
        first = self.sync(at(2 * 24 * 3600))
        self.remove()
        again = self.sync(at(2 * 24 * 3600))  # the same content as before
        self.assertEqual((again.action, again.status), ("create", "scheduled"))
        self.assertNotEqual(again.post_id, first.post_id)
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "scheduled"])
        self.assertEqual((self.entry()["post_id"], self.entry()["message"]), (again.post_id, None))
        self.assertEqual(self.sync(at(2 * 24 * 3600)).action, "unchanged")

    def test_a_post_being_sent_is_not_cancelled_and_the_sync_asks_for_a_rerun(self):
        first = self.sync(at(2 * 24 * 3600))
        self.store.update_post(first.post_id, status="publishing")
        r = self.remove()
        self.assertEqual((r.action, r.status, r.post_id), ("skipped", "publishing", first.post_id))
        self.assertIn("run the sync again", r.message)
        self.assertEqual(self.store.get_post(first.post_id)["status"], "publishing")

    def test_a_file_another_sync_is_making_now_is_skipped(self):
        self.assertTrue(self.store.claim_new(self.user, SOURCE, PATH, self.platform.id, "h"))
        r = self.remove()
        self.assertEqual((r.action, r.status), ("skipped", "syncing"))
        self.assertIn("Another sync of this file is in progress", r.message)
        self.assertEqual(self.entry()["status"], "syncing")

    def test_an_entry_without_a_post_has_nothing_to_cancel(self):
        self.store.put_entry(self.user, SOURCE, PATH, self.platform.id, content_hash="h", status="failed", message="refused")
        r = self.remove()
        self.assertEqual((r.action, r.status, r.post_id), ("removed", "failed", None))
        self.assertIn("nothing to cancel", r.message)
        self.assertEqual((self.entry()["status"], self.entry()["content_hash"]), ("failed", "h"))


class ARemovalTouchesOnlyItsOwnEntries(RemovedCase):
    def test_another_persons_post_of_the_same_path_is_untouched(self):
        other = self.store.upsert_user("local:other", "other", None, None)["id"]
        self.store.save_credentials(other, self.platform.id, {"k": "v"}, {"id": "2", "name": "other"})
        mine, theirs = self.sync(at(2 * 24 * 3600)), self.sync_as(other)
        self.assertNotEqual(mine.post_id, theirs.post_id)
        r = self.remove()
        self.assertEqual((r.action, r.status, r.post_id), ("removed", "cancelled", mine.post_id))
        self.assertEqual(self.store.get_post(theirs.post_id)["status"], "scheduled")
        self.assertEqual(self.store.get_entry(other, SOURCE, PATH, self.platform.id)["status"], "scheduled")

    def test_the_same_path_in_another_repository_is_untouched(self):
        elsewhere = "github.com/acme/other"
        mine, there = self.sync(at(2 * 24 * 3600)), self.sync_as(self.user, source=elsewhere)
        r = self.remove()
        self.assertEqual((r.action, r.status, r.post_id), ("removed", "cancelled", mine.post_id))
        self.assertEqual(self.store.get_post(there.post_id)["status"], "scheduled")
        self.assertEqual(self.store.get_entry(self.user, elsewhere, PATH, self.platform.id)["status"], "scheduled")
        back = self.remove(source=elsewhere)
        self.assertEqual((back.action, back.post_id), ("removed", there.post_id))


class ARemovalRacingAnEdit(RemovedCase):
    """Two syncs of the same path at once: one removes the file, one replaces its scheduled post with an edit (#275).
    Each cancels through ``cancel_if_pending``, one conditional write under the transaction's lock, so exactly one
    moves the post and the other finds it moved and asks for a re-run; the same write keeps the scheduler's claim and a
    person's cancel out (``test_sync_cancel_claim``). The loser is injected at that write: the competitor runs
    first, inside the loser's sync, just before its cancel."""

    def race(self, loser, winner):
        real, ran = self.store.cancel_if_pending, []

        def just_before_the_cancel(*args, **kwargs):
            if not ran:  # the winner's own cancel comes through here too: it goes straight to the write
                ran.append(True)
                other = threading.Thread(target=winner)  # another request, on its own loop and connection lock
                other.start()
                other.join()
            return real(*args, **kwargs)

        with mock.patch.object(self.store, "cancel_if_pending", just_before_the_cancel):
            result = loser()
        self.assertEqual(ran, [True], "the competitor never ran")
        return result

    def test_an_edit_that_cancels_first_wins_and_the_removal_asks_for_a_rerun(self):
        first = self.sync(at(2 * 24 * 3600))
        edit: dict[str, SyncResult] = {}
        r = self.race(self.remove, lambda: edit.update(result=self.sync(at(2 * 24 * 3600), body="Edited.")))
        self.assertEqual((edit["result"].action, edit["result"].status), ("update", "scheduled"))
        replacement = edit["result"].post_id
        self.assertEqual((r.action, r.status, r.post_id), ("skipped", "cancelled", first.post_id))
        self.assertIn("run the sync again", r.message)
        self.assertEqual(self.store.get_post(replacement)["status"], "scheduled", "the removal cancelled a post it never read")
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("scheduled", replacement))
        rerun = self.remove()
        self.assertEqual((rerun.action, rerun.status, rerun.post_id), ("removed", "cancelled", replacement))
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "cancelled"])

    def test_a_removal_that_cancels_first_wins_and_the_edit_makes_no_replacement(self):
        first = self.sync(at(2 * 24 * 3600))
        removal: dict[str, SyncResult] = {}
        r = self.race(lambda: self.sync(at(2 * 24 * 3600), body="Edited."), lambda: removal.update(result=self.remove()))
        self.assertEqual((removal["result"].action, removal["result"].status), ("removed", "cancelled"))
        self.assertEqual((r.action, r.post_id), ("skipped", first.post_id))
        self.assertIn("run the sync again", r.message)
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled"], "a replacement was made for a removed file")
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("cancelled", first.post_id))
        asyncio.run(self.publisher.run_due(now=at(3 * 24 * 3600).timestamp()))
        self.assertEqual(self.platform.sent, [])


class TheRequestNeedsFilesOrRemoved(unittest.TestCase):
    def test_neither_is_refused(self):
        with self.assertRaisesRegex(ValidationError, "files or removed is required"):
            SyncRequest(source=SOURCE)
        with self.assertRaisesRegex(ValidationError, "files or removed is required"):
            SyncRequest.model_validate({"source": SOURCE, "files": [], "removed": []})

    def test_a_path_in_both_is_refused(self):
        with self.assertRaisesRegex(ValidationError, "both synced and removed: posts/a.md"):
            SyncRequest(source=SOURCE, files=[SyncFile(path="posts/a.md", markdown="x")], removed=["posts/b.md", "posts/a.md"])

    def test_removed_alone_is_a_request(self):
        req = SyncRequest.model_validate({"source": SOURCE, "removed": ["posts/a.md"]})
        self.assertEqual((req.files, req.removed), ([], ["posts/a.md"]))

    def test_removed_has_the_bounds_of_a_file_path(self):
        self.assertEqual(SyncRequest(source=SOURCE, removed=["x" * 500]).removed, ["x" * 500])
        with self.assertRaisesRegex(ValidationError, "removed.0"):
            SyncRequest(source=SOURCE, removed=[""])
        with self.assertRaisesRegex(ValidationError, "removed.0"):
            SyncRequest(source=SOURCE, removed=["x" * 501])
        with self.assertRaisesRegex(ValidationError, "at most 50 items"):
            SyncRequest(source=SOURCE, removed=[f"{i}.md" for i in range(51)])
        schema = SyncRequest.model_json_schema()  # the same bounds in the contract, so clients see one rule
        bounds = {k: schema["properties"]["removed"]["items"][k] for k in ("minLength", "maxLength")}
        self.assertEqual(bounds, {k: schema["$defs"]["SyncFile"]["properties"]["path"][k] for k in bounds})
        self.assertNotIn("files", schema["required"])


if __name__ == "__main__":
    unittest.main()
