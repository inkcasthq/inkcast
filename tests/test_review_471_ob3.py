# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of PR #493 (issue #471): each class fails on bbf56cfe and passes with its fix.

1. A retry on a refused day trusted today's copy without reading it: a copy that no longer matches its manifest's
   checksum (changed after it was taken) was returned, ``copied=False``, and the day was left with no good copy.
2. The schedule test's cron reader took ``*/01`` for a restricted field; robfig/cron v3.0.1 (the Kubernetes CronJob
   controller's ParseStandard) takes it for a star, which makes the two day fields combine by AND, not OR.
3. The schedule test read the keep from the ConfigMap only; ``inkcast backup`` takes ``--keep`` in the CronJob's args
   (abbreviated too) and INKCAST_BACKUP_KEEP in its env over it, so a weekly schedule with ``--keep 7`` passed the test
   and is refused on every run."""

import copy
import shutil
import unittest

import test_471_backup_refusals as t471
from test_backup_clock import JUMP, KEEP, FiveDailyBackups
from test_openfga_component import one, render

from inkcast.libs.db.backup import BackupError, list_backups, take_backup, verify_backup


class ARetryFindsTodaysCopyChangedSinceItWasTaken(FiveDailyBackups):
    def test_the_retry_takes_a_good_copy_and_the_next_retry_none(self):
        first = take_backup(self.db, self.backups, keep=KEEP, now=JUMP)
        self.assertTrue(first.refused)
        with first.path.open("r+b") as f:                     # the volume changed a page after the copy was checked
            f.seek(4096)
            f.write(b"\xff" * 4096)
        with self.assertRaisesRegex(BackupError, "checksum"):
            verify_backup(first.path)
        retry = take_backup(self.db, self.backups, keep=KEEP, now=JUMP + 60)
        self.assertEqual((retry.copied, bool(retry.refused)), (True, True), "the day's only copy is broken: take one")
        verify_backup(retry.path)
        again = take_backup(self.db, self.backups, keep=KEEP, now=JUMP + 120)
        self.assertEqual((again.copied, again.name), (False, retry.name), "one good copy a day, as before")
        self.assertEqual(len(list_backups(self.backups, now=JUMP)), 7)


class TheCronReaderAgainstRobfig(unittest.TestCase):
    def test_a_step_of_01_after_a_star_keeps_the_star(self):
        # robfig/cron v3.0.1 parser.go getRange: "*" keeps its star bit unless the step, read as a number, is above 1.
        self.assertEqual(t471.run_days("17 3 */01 * 0"), t471.run_days("17 3 * * 0"), "weekly, as robfig reads it")
        self.assertEqual(t471.run_days("17 3 8 * */01"), t471.run_days("17 3 8 * *"), "the 8th, as robfig reads it")


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's test job has it)")
class AKeepTheCronJobSetsItself(unittest.TestCase):
    def test_a_keep_in_the_cronjob_s_args_or_env_fails_the_binding(self):
        docs = render("environments/staging")
        spec = one(docs, "CronJob", "inkcast-backup")["spec"]
        spec["schedule"] = "17 3 * * 0"                         # weekly: 7 days between two runs
        self.assertEqual(t471.refusal(docs), "", "keep 14 from the ConfigMap: every weekly run prunes")
        for args, env in ((["--keep", "7"], []), (["--kee=7"], []), (["--k", "7"], []),
                          ([], [{"name": "INKCAST_BACKUP_KEEP", "value": "7"}])):
            with self.subTest(args=args, env=env):
                changed = copy.deepcopy(docs)
                (container,) = one(changed, "CronJob", "inkcast-backup")["spec"]["jobTemplate"]["spec"]["template"][
                    "spec"]["containers"]
                container["args"] += args
                container["env"] += env
                self.assertTrue(t471.refusal(changed), "inkcast backup runs with keep 7: every weekly run is refused")


if __name__ == "__main__":
    unittest.main()
