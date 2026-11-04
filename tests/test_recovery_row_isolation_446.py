# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #446: one stuck row's recovery error no longer stops the pass for every row claimed after it.

``Publisher.run_due`` recovered the stuck rows it claimed one after another, and an exception raised for one row ended
the pass: every row after it, other people's posts included, stayed 'recovering' until the next stuck pass, which hit
the same row again. Within INKCAST_PUBLISH_RECOVERY_MAX_HOURS that held up recovery for everyone for up to a day (PR
#442 ended only the row past the cap). The case here: a person's credentials sealed under a previous INKCAST_SECRET_KEY,
which ``Sealer.unseal`` refuses, read by ``confirm`` outside its guard. ``FindingOneRowsRecoveryErrorDoesNotStopThePass``
failed on main (``RuntimeError: Stored credentials can't be decrypted`` out of ``run_due``, the colleague's row never
recovered); ``HeldAStoreErrorStillStopsThePass`` pins what holds, as a send's does."""

import sqlite3
import unittest

from inkcast.libs.db import Store
from inkcast.libs.observability import EVENTS, Metrics
from inkcast.libs.pipeline import STUCK_AFTER

from test_person_decides import Harness


class TwoPeoplesStuckRows(Harness):
    def setUp(self):
        super().setUp()
        # The on-call person's credentials were sealed under another INKCAST_SECRET_KEY: this store can't read them.
        other = Store(self.path, "j" * 40)
        self.addCleanup(other.close)
        other.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
        with self.assertRaises(RuntimeError):                                 # the premise
            self.store.get_credentials(self.user, self.platform.id)
        self.colleague = self.store.upsert_user("local:colleague", "colleague", None, None)["id"]
        self.store.save_credentials(self.colleague, self.platform.id, {"k": "v"}, {"id": "2", "name": "colleague"})
        # Both sends were interrupted; the on-call person's row is claimed first. Both are well inside the cap.
        self.first = self.post("publishing", "a")
        self.second = self.post("publishing", "b", user_id=self.colleague)
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?", STUCK_AFTER + 1)
        self.publisher.metrics = Metrics()

    def recovery_failures(self) -> float:
        return self.publisher.metrics.snapshot().get("inkcast_publish_total", {}).get(
            (("platform", "findable"), ("status", "recovery_failed")), 0)


class FindingOneRowsRecoveryErrorDoesNotStopThePass(TwoPeoplesStuckRows):
    def test_the_colleagues_row_is_recovered_in_the_same_pass_and_the_failed_row_waits_for_the_next(self):
        with self.assertLogs("inkcast.libs.pipeline", level="ERROR") as logged:
            outcomes = self.tick()                                            # raised on main, at the first row
        failed = [r for r in logged.records if getattr(r, "event", None) == "publish.recovery.failed"]
        self.assertEqual([(r.post_id, r.platform) for r in failed], [(self.first, "findable")],
                         "the failed row is not named by its id")
        self.assertIn(self.first, failed[0].getMessage())
        self.assertIn("publish.recovery.failed", EVENTS)
        self.assertEqual(self.recovery_failures(), 1)

        second = self.store.get_post(self.second)
        self.assertNotEqual(second["status"], "recovering", "the colleague's row waited for the next pass")
        self.assertIn(("post.recovered", self.colleague, {"platform": "findable", "status": "scheduled"}),
                      self.events(self.second))
        self.assertEqual([o.post_id for o in outcomes], [self.second])

        first = self.store.get_post(self.first)
        self.assertEqual(first["status"], "recovering", "the failed row was moved on without the platform's answer")
        self.assertIsNotNone(first["recovering_since"], "the cap's clock was dropped")
        self.assertEqual([t for t, _, _ in self.events(self.first)], [], "the failed row recorded an event")

        self.tick(later=STUCK_AFTER + 1)                                      # the next pass: the same row, the same error
        self.assertEqual((self.status(self.first), self.recovery_failures()), ("recovering", 2))

        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})   # reconnected
        self.tick(later=2 * (STUCK_AFTER + 1))
        self.assertEqual(self.status(self.first), "scheduled", "a row whose error is gone was not recovered")
        self.assertEqual(self.recovery_failures(), 2)


class HeldAStoreErrorStillStopsThePass(TwoPeoplesStuckRows):
    def test_the_database_failing_under_one_row_ends_the_pass_as_it_ends_a_send(self):
        """A store error says nothing about one row: the next row's write would fail the same way. The tick stops
        (``scheduler.tick.failed``) and the next stuck pass takes every row again."""
        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})   # readable
        real = self.store.update_post

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        self.store.update_post = locked
        with self.assertRaises(sqlite3.OperationalError):
            self.tick()
        self.store.update_post = real
        self.assertEqual((self.status(self.first), self.status(self.second)), ("recovering", "recovering"))
        self.assertEqual(self.recovery_failures(), 0)


if __name__ == "__main__":
    unittest.main()
