# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""One run on a clock that jumped ahead never removes every earlier backup (issue #405).

Retention is by age (#112), so a run whose clock was a year ahead found every backup a year old and removed them all,
leaving its own copy, dated a year on, as the only restore point; with the clock right again that copy stayed until its
own date plus ``keep``. Now a run whose newest backup from before today has aged out (``keep`` or more days old)
removes nothing and says why (``Backup.refused``; ``inkcast backup`` exits 1 after its ``backup.done`` line, so the Job
fails and InkcastBackupMissing fires), and a copy dated after today ages out with the day its run followed
(``previous`` in its manifest), the day the real history would have. The clock is injected (``now``), never slept for."""

import json
import logging
import pathlib
import shutil
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

from inkcast.libs.db import Store
from inkcast.libs.db import backup as module
from inkcast.libs.db.backup import list_backups, take_backup

KEEP = 14                   # the CronJob's default
DAY = 86400
YEAR = 365 * DAY
# The CronJob's hour, 03:17 UTC, on a fixed day: every stamp below is a known date.
DAY0 = datetime(2026, 11, 1, 3, 17, tzinfo=timezone.utc).timestamp()
JUMP = DAY0 + 5 * DAY + YEAR    # the sixth day's run, on a clock a year ahead


def stamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def names(paths: list[pathlib.Path]) -> list[str]:
    return [p.name for p in paths]


class FiveDailyBackups(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "data" / "inkcast.db"
        self.db.parent.mkdir()
        self.store = Store(str(self.db), "k" * 40)
        self.addCleanup(self.store.close)
        self.store.upsert_user("local:owner", "owner", None, None)
        self.backups = self.dir / "backups"
        self.days = [take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + i * DAY) for i in range(5)]

    def manifest(self, path: pathlib.Path) -> dict:
        return json.loads(path.with_suffix(".json").read_text())


class AClockAYearAhead(FiveDailyBackups):
    def test_one_run_removes_nothing_and_says_why(self):
        b = take_backup(self.db, self.backups, keep=KEEP, now=JUMP)
        self.assertEqual((b.removed_backups, b.removed_copies), (0, 0))
        self.assertIn(self.days[-1].name, b.refused)
        self.assertIn("366 days old, so every earlier backup has aged out (14 day(s) kept)", b.refused)
        self.assertIn("clock", b.refused)
        self.assertEqual(names(list_backups(self.backups, now=JUMP)), names(d.path for d in self.days) + [b.name])
        self.assertEqual(b.manifest["previous"], stamp(DAY0 + 4 * DAY), "the copy records the day it followed")

    def test_a_job_retried_minutes_later_refuses_again(self):
        # Kubernetes retries a failed Job (backoffLimit 2), each attempt a backup on the same clock: today's copies do
        # not count as the newest earlier backup, so each attempt judges the clock by the days before it.
        attempts = [take_backup(self.db, self.backups, keep=KEEP, now=JUMP + i * 60) for i in range(3)]
        self.assertEqual([a.removed_backups for a in attempts], [0, 0, 0])
        self.assertTrue(all(a.refused for a in attempts))
        self.assertTrue(all(d.path.exists() for d in self.days))
        # Each copy records the one before it, so all three lead back to the day before the jump (_dated follows them).
        self.assertEqual([a.manifest["previous"] for a in attempts],
                         [stamp(DAY0 + 4 * DAY), stamp(JUMP), stamp(JUMP + 60)])

    def test_the_command_exits_1_after_a_good_backup_so_the_job_fails_and_the_alert_fires(self):
        from inkcast.backup import backup_main
        with mock.patch.object(module, "time", types.SimpleNamespace(time=lambda: JUMP)), \
                mock.patch("inkcast.backup._logging"), self.assertLogs("inkcast.backup", logging.INFO) as logs:
            code = backup_main(["--database", str(self.db), "--to", str(self.backups), "--keep", str(KEEP)])
        self.assertEqual(code, 1)
        self.assertEqual([getattr(r, "event", "") for r in logs.records], ["backup.done", "backup.retention.refused"])
        done, refused = logs.records
        self.assertEqual((done.removed_backups, done.removed_copies), (0, 0))
        self.assertEqual(refused.levelno, logging.ERROR)
        self.assertIn("nothing was removed", refused.getMessage())
        self.assertEqual(len(list_backups(self.backups, now=JUMP)), 6, "the backup itself was kept")

    def test_with_the_clock_right_again_the_copy_ages_out_with_the_day_it_followed(self):
        """How the future-dated copy is handled: it stays a restore point, is never what ``--latest`` picks over a copy
        of a sane day, and goes the day a backup of the day before the jump goes (``previous`` in its manifest), 14 days
        after that day rather than a year after its own date; the daily series is then as if the jump never happened."""
        future = take_backup(self.db, self.backups, keep=KEEP, now=JUMP)
        removed = {}
        for day in range(5, 19):                                        # the clock right: daily runs, day 5 to day 18
            b = take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + day * DAY)
            self.assertEqual(b.refused, "", f"day {day}")
            removed[day] = b.removed_backups
            self.assertEqual(list_backups(self.backups, now=DAY0 + day * DAY)[-1], b.path,
                             f"day {day}: --latest takes today's copy, not the one dated a year on")
            if day < 18:
                self.assertTrue(future.path.exists(), f"day {day}: within 14 days of day 4, the day it followed")
        self.assertFalse(future.path.exists(), "day 18: 14 days after day 4, it went with day 4's own copy")
        self.assertFalse(future.path.with_suffix(".json").exists())
        self.assertEqual(removed, {**{d: 0 for d in range(5, 14)}, **{d: 1 for d in range(14, 18)}, 18: 2})
        self.assertEqual(names(list_backups(self.backups, now=DAY0 + 18 * DAY)),
                         [f"inkcast-{stamp(DAY0 + d * DAY)}.db" for d in range(5, 19)])

    def test_the_next_day_s_run_on_the_same_wrong_clock_is_not_covered_the_failed_job_is(self):
        """The limit, stated: a clock still a year ahead at the next day's run finds the first run's copy a day old and
        sane, and the real history a year old, and removes it, as a run after a year-long gap would. The files cannot
        tell the two apart; the first run's failed Job, and the alert it raises within hours, are the day to fix the
        clock. The copies that run leaves are good backups of the data as it is."""
        first = take_backup(self.db, self.backups, keep=KEEP, now=JUMP)
        self.assertTrue(first.refused)
        second = take_backup(self.db, self.backups, keep=KEEP, now=JUMP + DAY)
        self.assertEqual((second.refused, second.removed_backups), ("", 5))
        self.assertEqual(names(list_backups(self.backups, now=JUMP + DAY)), [first.name, second.name])


class AClockBehind(FiveDailyBackups):
    def test_removes_nothing_as_before_and_keeps_the_newest_copy_last(self):
        # Two days behind the newest backup (a minute past that day's own run, so the names differ): nothing has aged,
        # and the copies that look future-dated are dated by the day they followed, never earlier than the real series.
        behind = DAY0 + 2 * DAY + 60
        b = take_backup(self.db, self.backups, keep=KEEP, now=behind)
        self.assertEqual((b.removed_backups, b.refused), (0, ""))
        self.assertTrue(all(d.path.exists() for d in self.days))
        self.assertEqual(list_backups(self.backups, now=behind)[-1], b.path)
        self.assertEqual(len(list_backups(self.backups, now=behind)), 6)


class AGapInBackups(FiveDailyBackups):
    """The trade-off: a gap longer than ``keep`` days and a clock that far ahead look the same from the files."""

    def test_longer_than_keep_days_is_refused_once_then_pruned_the_next_day(self):
        b = take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + 19 * DAY)    # 15 days after the last one
        self.assertEqual(b.removed_backups, 0)
        self.assertTrue(b.refused.startswith(f"the newest earlier backup, {self.days[-1].name}, is 15 days old"), b.refused)
        self.assertEqual(len(list_backups(self.backups)), 6)
        c = take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + 20 * DAY)    # a day on: a gap of one day
        self.assertEqual((c.removed_backups, c.refused), (5, ""))
        self.assertEqual(names(list_backups(self.backups)), [b.name, c.name])

    def test_of_exactly_keep_days_is_refused_once_too(self):
        # 14 days after the last one: every backup has aged out, as on a clock 13 days ahead (review of #457).
        b = take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + 18 * DAY)
        self.assertEqual(b.removed_backups, 0)
        self.assertTrue(b.refused.startswith(f"the newest earlier backup, {self.days[-1].name}, is 14 days old"), b.refused)
        c = take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + 19 * DAY)
        self.assertEqual((c.removed_backups, c.refused), (5, ""))
        self.assertEqual(names(list_backups(self.backups)), [b.name, c.name])

    def test_of_keep_minus_one_days_prunes_at_once_as_before(self):
        b = take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + 17 * DAY)    # 13 days after the last one
        self.assertEqual((b.removed_backups, b.refused), (4, ""))
        self.assertEqual(names(list_backups(self.backups)), [self.days[-1].name, b.name])

    def test_keep_1_keeps_only_today_s_copies_as_before(self):
        backups = self.dir / "daily"
        take_backup(self.db, backups, keep=1, now=DAY0)
        b = take_backup(self.db, backups, keep=1, now=DAY0 + DAY)
        self.assertEqual((b.removed_backups, b.refused), (1, ""))
        self.assertEqual(names(list_backups(backups)), [b.name])


class ACopyDatedAfterToday(FiveDailyBackups):
    def test_without_previous_or_with_a_broken_chain_is_left_as_before(self):
        # A manifest from before this change, and one whose chain leads to a copy that is gone: each copy keeps its own
        # date, and is left alone until that date plus keep, as before.
        old = take_backup(self.db, self.backups, keep=KEEP, now=JUMP)
        manifest = self.manifest(old.path)
        del manifest["previous"]
        old.path.with_suffix(".json").write_text(json.dumps(manifest))
        broken = take_backup(self.db, self.backups, keep=KEEP, now=JUMP + 60)
        manifest = self.manifest(broken.path)
        manifest["previous"] = stamp(DAY0 - 30 * DAY)                             # a backup that is gone
        broken.path.with_suffix(".json").write_text(json.dumps(manifest))
        for day in range(5, 25):
            take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + day * DAY)
        self.assertTrue(old.path.exists() and broken.path.exists())
        self.assertEqual(len(list_backups(self.backups)), 16)                    # days 11 to 24, and the two


if __name__ == "__main__":
    unittest.main()
