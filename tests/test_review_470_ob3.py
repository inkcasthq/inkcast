# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of PR #491 (issue #470): what the PR states, against what the code does.

1. The runbook says two ``scheduler.housekeeping.wal_pending`` lines in a row mean the log has kept its bytes for an
hour or more. Housekeeping runs HOUSEKEEPING_EVERY apart, so a person erased while a reader holds an older snapshot,
just before a run, is still in the log at that run and at the next: two lines, 30 minutes after the erasure."""

import pathlib
import unittest
from unittest import mock

from inkcast.libs.db import database
from inkcast.services import scheduler
from inkcast.services.scheduler import housekeeping_if_due
from test_erasure_wal import APersonWithContent, context, recorded

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "runbooks" / "backup-restore.md"
DEFERRED = "scheduler.housekeeping.wal_pending"
TICK = 20.0                     # the scheduler's default interval: housekeeping runs at the first tick that is due


class TwoPendingRunsInARow(APersonWithContent):
    def test_come_30_minutes_after_the_erasure_and_the_runbook_says_so(self):
        """A reader holds its snapshot throughout; she is erased a tick before housekeeping is due; the scheduler ticks
        every TICK seconds of a clock the test moves. When the second line is written, her bytes are still in the
        log, and they have been there HOUSEKEEPING_EVERY and one tick, not an hour."""
        clock = [1_800_000_000.0]
        ctx = context(self.store)
        lines, erased_at = [], None
        with mock.patch("time.time", lambda: clock[0]), mock.patch.object(database, "WAL_CHECKPOINT_WAIT_MS", 20), \
                recorded("inkcast.services.scheduler") as records:
            self.store.beat(scheduler.HOUSEKEEPING)
            ran_at = clock[0]
            while len(lines) < 2:
                clock[0] += TICK
                if erased_at is None and clock[0] + TICK - ran_at >= scheduler.HOUSEKEEPING_EVERY:
                    self.pinned_by_a_reader()                                 # a backup's copy, held from now on
                    self.assertEqual(self.store.erase_user(self.user["id"])["users"], 1)
                    erased_at = clock[0]
                before = len(records)
                if housekeeping_if_due(ctx):
                    ran_at = clock[0]
                    if any(getattr(r, "event", None) == DEFERRED for r in records[before:]):
                        lines.append((clock[0] - erased_at, self.holding()))
                self.assertLess(clock[0], 1_800_000_000.0 + 4 * 3600, "two lines within four hours")
        (first, held_first), (second, held_second) = lines
        self.assertIn("inkcast.db-wal", held_first)
        self.assertIn("inkcast.db-wal", held_second, "her bytes are still in the log at the second line")
        self.assertEqual(first, TICK, "the first line, at the run just after her erasure")
        self.assertEqual(second, scheduler.HOUSEKEEPING_EVERY + TICK, "the second line, 30 minutes and a tick after her")
        (sentence,) = [s for s in RUNBOOK.read_text().split(". ") if "two in a row" in s]
        self.assertIn("two in a row mean the log has kept its bytes for 30 minutes or more", sentence)


if __name__ == "__main__":
    unittest.main()
