# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #229: what the review of #213 left on Git sync's error paths.

1. A double fault on the replace path: the replacement's row exists and names nothing but its post (``on_record``),
   and both later writes of the entry fail (or the process stops there). The entry kept the replaced post's content
   hash, so once the claim expired the next sync took the unchanged file for an edit: it replaced the replacement
   again, and made again a replacement a person had cancelled. The entry now takes the content hash with the post it
   names, in the row's own transaction. The third symptom, a published replacement read as `stale`, goes with it.
3. A send the platform refused, whose ``failed`` write was locked: the entry read "internal error" instead of the
   platform's reason. It keeps the reason now.

The locked writes are injected on the writes they are about, as in ``test_sync_error_after_post``."""

import sqlite3
import unittest
from unittest import mock

from inkcast.libs.db.ledger import CLAIM_TTL
from inkcast.libs.platforms import PlatformError

from test_sync_error_after_post import SyncCase, at


def every_write_of_the_outcome(*args, **kwargs) -> bool:
    """The writes after ``publish()`` returned or raised: the outcome's, then ``_after_error``'s. Not ``on_record``'s."""
    return "status" in kwargs


class Locked:
    """``put_entry`` raising "database is locked" on the first ``times`` writes ``when`` matches (every one, for None),
    calling through otherwise."""

    def __init__(self, real, when, times: int | None = None):
        self.real, self.when, self.times, self.raised = real, when, times, 0

    def __call__(self, *args, **kwargs):
        if self.when(*args, **kwargs) and (self.times is None or self.raised < self.times):
            self.raised += 1
            raise sqlite3.OperationalError("database is locked")
        return self.real(*args, **kwargs)


class FindingADoubleFaultOnTheReplacePath(SyncCase):
    def replaced_twice_failing(self) -> tuple[dict, dict]:
        """A scheduled post, its body edited; the replacement is made, and every write of the entry after it fails.
        The claim then expires. The replaced post and the replacement."""
        when = at(2 * 24 * 3600)
        first = self.sync(when)
        locked = Locked(self.store.put_entry, every_write_of_the_outcome)
        with mock.patch.object(self.store, "put_entry", locked), self.assertRaises(sqlite3.OperationalError):
            self.sync(when, body="Better body.")
        self.assertEqual(locked.raised, 2, "the outcome's write and _after_error's")
        replaced, replacement = self.posts()
        self.assertEqual((replaced["id"], replaced["status"], replacement["status"]), (first.post_id, "cancelled",
                                                                                    "scheduled"))
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("syncing", replacement["id"]))
        self.store._exec("UPDATE ledger SET synced_at = synced_at - ?", CLAIM_TTL + 1)
        return replaced, replacement

    def test_the_unchanged_file_leaves_the_replacement_alone(self):
        _, replacement = self.replaced_twice_failing()
        again = self.sync(at(2 * 24 * 3600), body="Better body.")
        self.assertEqual((again.action, again.post_id), ("unchanged", replacement["id"]), "replaced a second time")
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "scheduled"])

    def test_a_replacement_a_person_cancelled_is_not_made_again(self):
        _, replacement = self.replaced_twice_failing()
        self.publisher.cancel(self.user, replacement["id"])
        again = self.sync(at(2 * 24 * 3600), body="Better body.")
        self.assertEqual((again.action, again.status), ("unchanged", "cancelled"), "the person's cancel was undone")
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "cancelled"])

    def test_a_published_replacement_is_not_read_as_stale(self):
        """The third symptom: the edit removes publish_at, so the replacement is a "now" post the sync itself sends;
        both writes of the entry after it fail, and the claim expires."""
        self.sync(at(2 * 24 * 3600))
        locked = Locked(self.store.put_entry, every_write_of_the_outcome)
        with mock.patch.object(self.store, "put_entry", locked), self.assertRaises(sqlite3.OperationalError):
            self.sync(None, body="Better body.")
        self.assertEqual(locked.raised, 2, "the outcome's write and _after_error's")
        replaced, replacement = self.posts()
        self.assertEqual((replaced["status"], replacement["status"], self.platform.sent),
                         ("cancelled", "published", ["Better body."]))
        self.store._exec("UPDATE ledger SET synced_at = synced_at - ?", CLAIM_TTL + 1)
        again = self.sync(None, body="Better body.")
        self.assertEqual((again.action, again.status, again.post_id), ("unchanged", "published", replacement["id"]),
                         "the published replacement read as stale")
        self.assertIsNone(self.entry()["message"])


class FindingWhatTheGuideSaysOfARerun(SyncCase):
    def test_a_file_whose_failed_run_could_not_record_its_error_is_held_for_claim_ttl(self):
        """docs/git-publishing.md: no row made, and the error's own record locked too. The claim holds the file: a
        re-run inside CLAIM_TTL reports another sync running, and one after it makes the post, once."""
        locked = Locked(self.store.put_entry, lambda *a, **k: set(k) in ({"post_id", "content_hash"},
                                                                       {"status", "message"}))
        with mock.patch.object(self.store, "put_entry", locked), self.assertRaises(sqlite3.OperationalError):
            self.sync()
        self.assertEqual((locked.raised, self.posts(), self.entry()["status"]), (2, [], "syncing"))
        held = self.sync()
        self.assertEqual((held.action, held.message), ("skipped", "Another sync of this file is in progress."))
        self.store._exec("UPDATE ledger SET synced_at = synced_at - ?", CLAIM_TTL + 1)
        made = self.sync()
        self.assertEqual((made.action, made.status, self.platform.sent), ("create", "published", ["Body."]))


class FindingTheRefusalReasonKept(SyncCase):
    def test_a_refused_send_whose_failed_write_is_locked_keeps_the_platform_reason(self):
        self.platform.error = PlatformError("Findable says: the title is too long.")
        locked = Locked(self.store.put_entry, lambda *a, **k: k.get("status") == "failed", times=1)
        with mock.patch.object(self.store, "put_entry", locked), self.assertRaises(sqlite3.OperationalError):
            self.sync()
        self.assertEqual(locked.raised, 1)
        self.assertEqual(self.entry()["status"], "failed")
        self.assertIn("the title is too long", self.entry()["message"], "the platform's reason was lost")


if __name__ == "__main__":
    unittest.main()
