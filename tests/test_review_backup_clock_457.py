# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #457 (issue #405): clock jumps the refusal rule of the PR still lets remove every earlier backup.

* A clock ``keep - 1`` days ahead finds the newest earlier backup exactly ``keep`` days old: the PR prunes that ("a gap
  of exactly keep days prunes at once"), and every backup is ``keep`` or more days old, so one run removes every earlier
  restore point, the issue's failure, with no refusal and exit 0. With ``keep`` 2 that is a clock one day ahead."""

import pathlib
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from inkcast.libs.db import Store
from inkcast.libs.db.backup import list_backups, take_backup

DAY = 86400
DAY0 = datetime(2026, 11, 1, 3, 17, tzinfo=timezone.utc).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "data" / "inkcast.db"
        self.db.parent.mkdir()
        self.store = Store(str(self.db), "k" * 40)
        self.addCleanup(self.store.close)
        self.store.upsert_user("local:owner", "owner", None, None)

    def daily(self, keep: int, days: int) -> tuple[pathlib.Path, list]:
        """``days`` daily runs (the CronJob's 03:17 UTC) from DAY0, each with its retention, into a fresh directory."""
        backups = self.dir / f"backups-{keep}"
        return backups, [take_backup(self.db, backups, keep=keep, now=DAY0 + i * DAY) for i in range(days)]


class AClockKeepMinusOneDaysAhead(Base):
    def test_one_run_leaves_an_earlier_backup_or_removes_nothing(self):
        for keep in (2, 3, 14):
            with self.subTest(keep=keep):
                backups, runs = self.daily(keep, keep + 2)
                earlier = [r.path for r in runs if r.path.exists()]
                # The next day's run, on a clock keep - 1 days ahead: every copy it finds is keep or more days old.
                b = take_backup(self.db, backups, keep=keep, now=DAY0 + (keep + 2 + keep - 1) * DAY)
                left = [p.name for p in earlier if p.exists()]
                self.assertTrue(b.refused or left, f"keep {keep}: the run removed all {len(earlier)} earlier backups "
                                                   f"({b.removed_backups}) and was not refused")

    def test_keep_1_still_keeps_today_s_copies_only(self):
        backups, runs = self.daily(1, 3)
        self.assertEqual([r.refused for r in runs], ["", "", ""])
        self.assertEqual([r.removed_backups for r in runs], [0, 1, 1])
        self.assertEqual([p.name for p in backups.glob("inkcast-*.db")], [runs[-1].name])


class AClockAYearAheadForTwoRuns(Base):
    """The PR's stated limit: the second run on the wrong clock finds the first one's copy a day old and removes the
    real history, the backup the first copy's ``previous`` names included. With the clock right again the two copies
    must still be handled as the issue asks: they age out with the day they followed, and ``--latest`` never takes them
    over a copy of a sane day."""

    def test_once_the_clock_is_right_their_copies_age_out_and_are_never_latest(self):
        keep, year = 14, 365 * DAY
        backups, runs = self.daily(keep, 5)                                  # days 0 to 4
        first = take_backup(self.db, backups, keep=keep, now=DAY0 + 5 * DAY + year)
        second = take_backup(self.db, backups, keep=keep, now=DAY0 + 6 * DAY + year)
        self.assertTrue(first.refused)
        self.assertEqual((second.refused, second.removed_backups), ("", 5), "the PR's stated limit, as pinned there")
        for day in range(7, 7 + keep):                                       # the clock right from day 7
            now = DAY0 + day * DAY
            b = take_backup(self.db, backups, keep=keep, now=now)
            self.assertEqual(list_backups(backups, now=now)[-1].name, b.name, f"day {day}: --latest")
        self.assertFalse(first.path.exists() or second.path.exists(),
                         f"day {6 + keep}: {keep} days after day 4, the day both copies followed")


if __name__ == "__main__":
    unittest.main()
