# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of PR #493 (issue #471): each class fails on bbf56cfe and passes with its fix.

1. A retry on a refused day trusted today's copy without reading it: a copy that no longer matches its manifest's
   checksum (changed after it was taken) was returned, ``copied=False``, and the day was left with no good copy."""

import unittest

from test_backup_clock import JUMP, KEEP, FiveDailyBackups

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


if __name__ == "__main__":
    unittest.main()
