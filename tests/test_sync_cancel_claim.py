# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #224: a failed claim write right after the cancel lost a rescheduled post.

On the replace path (an edited scheduled post) the sync cancelled the post, then wrote its claim on the file as a
second write. When that write failed (``database is locked``), the entry was left 'scheduled' on a post that was now
cancelled; the next sync followed it to 'cancelled', and a schedule-only edit (same content) read as unchanged: the
post was never sent. Now the cancel and the claim are one transaction, so a failed claim leaves the post scheduled.

The failure is injected in SQL (a trigger refusing the claim's ledger write), so the test doesn't depend on which
method writes the claim."""

import asyncio
import contextlib
import sqlite3
import unittest
from typing import Iterator

from inkcast.libs.platforms import PlatformError

from test_sync_error_after_post import SyncCase, at


@contextlib.contextmanager
def refused_claims(store) -> Iterator[None]:
    """Every ledger write that makes an entry 'syncing' (the claim) fails while this is open."""
    for event in ("INSERT", "UPDATE"):
        store._exec(f"CREATE TEMP TRIGGER refuse_claim_{event.lower()} BEFORE {event} ON ledger "
                    "WHEN NEW.status = 'syncing' BEGIN SELECT RAISE(ABORT, 'database is locked'); END")
    try:
        yield
    finally:
        for event in ("insert", "update"):
            store._exec(f"DROP TRIGGER temp.refuse_claim_{event}")


class AFailedClaimAfterTheCancel(SyncCase):
    def live(self) -> list[dict]:
        return [p for p in self.posts() if p["status"] == "scheduled"]

    def test_a_schedule_only_edit_whose_claim_fails_is_still_rescheduled(self):
        first = self.sync(at(2 * 24 * 3600))
        later = at(3 * 24 * 3600)
        with refused_claims(self.store), self.assertRaises(sqlite3.DatabaseError):
            self.sync(later)
        (post,) = self.posts()
        self.assertEqual((post["id"], post["status"]), (first.post_id, "scheduled"),
                         "cancelled though the claim that replaces it was never written")
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("scheduled", first.post_id))

        rerun = self.sync(later)
        live = self.live()
        self.assertEqual(len(live), 1, f"no scheduled post after the rerun ({rerun.action} {rerun.status}): lost")
        self.assertAlmostEqual(live[0]["run_at"], later.timestamp(), delta=1, msg="at the time as written")
        self.assertEqual((rerun.action, rerun.status, rerun.post_id), ("update", "scheduled", live[0]["id"]))
        self.assertEqual(self.sync(later).action, "unchanged")
        asyncio.run(self.publisher.run_due(now=later.timestamp() + 1))
        self.assertEqual((self.platform.sent, self.store.get_post(live[0]["id"])["status"]), (["Body."], "published"))

    def test_a_body_edit_whose_claim_fails_is_still_replaced(self):
        when = at(2 * 24 * 3600)
        first = self.sync(when)
        with refused_claims(self.store), self.assertRaises(sqlite3.DatabaseError):
            self.sync(when, body="Better body.")
        self.assertEqual(self.store.get_post(first.post_id)["status"], "scheduled")
        rerun = self.sync(when, body="Better body.")
        (live,) = self.live()
        self.assertEqual((rerun.action, rerun.post_id), ("update", live["id"]))
        asyncio.run(self.publisher.run_due(now=when.timestamp() + 1))
        self.assertEqual(self.platform.sent, ["Better body."])

    def test_a_post_a_person_cancelled_stays_cancelled(self):
        """What the fix keeps: a person's cancel isn't the sync's, so a schedule-only edit doesn't bring it back."""
        when = at(2 * 24 * 3600)
        first = self.sync(when)
        self.publisher.cancel(self.user, first.post_id)
        self.assertEqual((self.sync(when).action, self.entry()["status"]), ("unchanged", "cancelled"))
        moved = self.sync(at(3 * 24 * 3600))
        self.assertEqual((moved.action, moved.status), ("unchanged", "cancelled"))
        self.assertEqual(self.live(), [])


class TheCancelAndItsCompanionWrite(SyncCase):
    """``cancel_if_pending(also=...)``: the companion write runs in the cancel's transaction, only when it cancels."""

    def test_the_companion_runs_only_when_the_post_is_cancelled(self):
        first = self.sync(at(2 * 24 * 3600))
        calls: list[str] = []
        self.store.update_post(first.post_id, status="published")
        self.assertIsNone(self.store.cancel_if_pending(first.post_id, self.user, also=lambda c: calls.append("ran")))
        self.assertEqual(calls, [])
        with self.assertRaises(PlatformError):
            self.publisher.cancel(self.user, first.post_id, also=lambda c: calls.append("ran"))
        self.assertEqual(calls, [])

    def test_a_failed_companion_rolls_the_cancel_back_with_its_event(self):
        first = self.sync(at(2 * 24 * 3600))
        before = self.store._one("SELECT COUNT(*) AS n FROM outbox")["n"]

        def fails(c):
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            self.publisher.cancel(self.user, first.post_id, also=fails)
        self.assertEqual(self.store.get_post(first.post_id)["status"], "scheduled")
        self.assertEqual(self.store._one("SELECT COUNT(*) AS n FROM outbox")["n"], before, "post.cancelled was written")


if __name__ == "__main__":
    unittest.main()
