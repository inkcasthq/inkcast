# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #402 (issue #112): copies of the database that the age-out of backups and pre-restore copies misses.

* A backup killed midway leaves a whole copy of the database behind: ``.inkcast-<time>.db.partial`` (a SIGTERM at the
  Job's ``activeDeadlineSeconds``, an OOM kill: the CLI installs no handler, so the cleanup never runs), or the backup
  renamed into place without its manifest. Neither is in ``list_backups``, so neither ever aged out.
* The runbook's way back to a pre-restore copy (``inkcast backup --database <db>.before-restore-<time>``) made a
  backup named with the time it was taken, not the time its content is from: the copy, which holds whoever was erased
  after the restore, then lived ``INKCAST_BACKUP_KEEP`` days more as a backup."""

import pathlib
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timezone

from inkcast.libs.db import Store
from inkcast.libs.db.backup import BackupError, aside_copies, list_backups, restore_backup, take_backup

KEY = "k" * 40
DAY = 86400
DAY0 = datetime(2026, 11, 1, 3, 17, tzinfo=timezone.utc).timestamp()
KEEP = 14


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "data" / "inkcast.db"
        self.db.parent.mkdir()
        self.backups = self.dir / "backups"
        self.store = Store(str(self.db), KEY)
        self.addCleanup(lambda: self.store.close())
        self.store.upsert_user("local:owner", "owner", None, None)

    def holding(self, marker: str) -> list[str]:
        """Every file on either volume whose bytes hold ``marker``."""
        return sorted(str(p.relative_to(self.dir)) for p in self.dir.rglob("*")
                      if p.is_file() and marker.encode() in p.read_bytes())


class UnfinishedBackups(Base):
    def test_a_copy_a_killed_backup_left_ages_out_with_the_backups(self):
        first = take_backup(self.db, self.backups, keep=KEEP, now=DAY0)
        stamp = first.name[len("inkcast-"):-len(".db")]
        # The two things a backup killed midway leaves, both from the day before DAY0, each a whole copy.
        partial = self.backups / f".inkcast-{stamp.replace('20261101', '20261031')}.db.partial"
        shutil.copyfile(first.path, partial)
        bare = self.backups / f"inkcast-{stamp.replace('20261101', '20261031').replace('T03', 'T04')}.db"
        shutil.copyfile(first.path, bare)
        self.assertNotIn(bare, list_backups(self.backups), "no manifest: outside the rotation")
        take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + (KEEP - 2) * DAY)
        self.assertTrue(partial.exists() and bare.exists(), "within keep days: left alone")
        take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + (KEEP - 1) * DAY)
        self.assertFalse(partial.exists(), "a partial copy keep days old went")
        self.assertFalse(bare.exists(), "a backup without its manifest keep days old went")
        self.assertTrue(first.path.exists(), "the backup from DAY0 is one day younger: kept")

    def test_an_unfinished_copy_whose_name_carries_no_day_is_left_alone(self):
        self.backups.mkdir()
        odd = self.backups / ".inkcast-by-hand.db.partial"
        odd.write_bytes(b"x")
        take_backup(self.db, self.backups, keep=1, now=DAY0 + 400 * DAY)
        self.assertTrue(odd.exists())


class TheWayBackToAPreRestoreCopy(Base):
    def test_a_backup_of_the_copy_ages_out_when_the_copy_would_have(self):
        marker = "r402-" + uuid.uuid4().hex
        person = self.store.upsert_user(f"oidc:kc#{marker}", marker, f"{marker}@example.test", marker)
        b = take_backup(self.db, self.backups, keep=KEEP, now=DAY0)
        self.store.close()
        aside = restore_backup(b.path, self.db, now=DAY0 + 3600)          # the copy holds the person
        self.store = Store(str(self.db), KEY)
        self.assertEqual(self.store.erase_user(person["id"])["users"], 1)  # erased five days after the restore
        erased_at = DAY0 + 5 * DAY
        # The runbook's way back, on day 13: a backup of the copy, to restore from. The daily backups go on meanwhile
        # (days 7 and 14): a gap longer than KEEP days is what #405 refuses to prune after.
        take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + 7 * DAY)
        back = take_backup(aside, self.backups, keep=KEEP, now=DAY0 + 13 * DAY)
        self.assertIn(marker.encode(), back.path.read_bytes())
        take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + 14 * DAY)
        # The promise: within KEEP days of the erasure, no copy on either volume holds the person.
        take_backup(self.db, self.backups, keep=KEEP, now=erased_at + KEEP * DAY)
        self.store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")       # the live WAL's old frames: not this test's
        self.assertEqual(aside_copies(self.db), [], "the copy itself aged out")
        self.assertEqual(self.holding(marker), [], f"{KEEP} days after the erasure, no copy holds the person")

    def test_a_backup_of_a_copy_that_has_aged_out_is_refused(self):
        b = take_backup(self.db, self.backups, keep=KEEP, now=DAY0)
        self.store.close()
        aside = restore_backup(b.path, self.db, now=DAY0 + 3600)
        self.store = Store(str(self.db), KEY)
        with self.assertRaises(BackupError):
            take_backup(aside, self.backups, keep=KEEP, now=DAY0 + KEEP * DAY)
        self.assertEqual([p.name for p in list_backups(self.backups)], [b.name], "nothing was added")


if __name__ == "__main__":
    unittest.main()
