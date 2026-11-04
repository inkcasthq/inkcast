# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of PR #491 (issue #470): what the PR states, against what the code does.

1. The runbook says two ``scheduler.housekeeping.wal_pending`` lines in a row mean the log has kept its bytes for an
hour or more. Housekeeping runs HOUSEKEEPING_EVERY apart, so a person erased while a reader holds an older snapshot,
just before a run, is still in the log at that run and at the next: two lines, 30 minutes after the erasure.

2. ``checkpoint_wal`` says its wait is the caller's thread sleeping, and that async code runs it in a worker thread.
The scheduler's loop calls housekeeping on the loop itself (``Publisher.run_forever``), and in the ``all`` process
(deploy/kustomize/base) that loop also serves every request: with a reader holding the log, the loop stood still for
the whole wait, 251.7 ms in the review's drive at the default WAL_CHECKPOINT_WAIT_MS. Here the wait is WAIT, as in
tests/test_470_erasure_checkpoint.py, so the loop standing still cannot pass for a race with the default wait."""

import asyncio
import pathlib
import threading
import unittest
from unittest import mock

from inkcast.libs.db import database
from inkcast.libs.pipeline import Publisher
from inkcast.services import scheduler
from inkcast.services.scheduler import housekeeping, housekeeping_if_due
from test_erasure_wal import APersonWithContent, context, recorded

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "runbooks" / "backup-restore.md"
DEFERRED = "scheduler.housekeeping.wal_pending"
TICK = 20.0                     # the scheduler's default interval: housekeeping runs at the first tick that is due
WAIT = 2.0


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


class HousekeepingOffTheLoop(APersonWithContent):
    def test_the_scheduler_s_loop_runs_while_housekeeping_s_checkpoint_waits(self):
        patch = mock.patch.object(database, "WAL_CHECKPOINT_WAIT_MS", int(WAIT * 1000))
        patch.start()
        self.addCleanup(patch.stop)
        reader = self.pinned_by_a_reader()
        started, finished, results = threading.Event(), threading.Event(), []
        real = self.store.checkpoint_wal

        def watched():
            started.set()
            try:
                results.append(real())
                return results[-1]
            finally:
                finished.set()
        self.store.checkpoint_wal = watched
        stop = asyncio.Event()

        def once():
            stop.set()
            housekeeping(context(self.store))

        async def run():
            async def meanwhile():                      # another coroutine of the loop: a request, in the "all" process
                while not started.is_set():
                    await asyncio.sleep(0.001)
                await asyncio.sleep(0.01)
                waiting = not finished.is_set()
                reader.execute("COMMIT")
                return waiting
            other = asyncio.ensure_future(meanwhile())
            await Publisher(self.store, {}).run_forever(0.01, stop, housekeeping=once)
            return await other

        self.assertTrue(asyncio.run(run()), "the loop ran while housekeeping's checkpoint waited for the reader")
        self.assertEqual([busy for busy, _, _ in results], [0], "then it cut the log, once the reader ended")
        self.assertEqual(self.wal_size(), 0)


if __name__ == "__main__":
    unittest.main()
