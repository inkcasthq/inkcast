# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #402 (issue #112): copies of the database that the age-out of backups and pre-restore copies misses.

* A backup killed midway leaves a whole copy of the database behind: ``.inkcast-<time>.db.partial`` (a SIGTERM at the
  Job's ``activeDeadlineSeconds``, an OOM kill: the CLI installs no handler, so the cleanup never runs), or the backup
  renamed into place without its manifest. Neither is in ``list_backups``, so neither ever aged out."""

import pathlib
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from inkcast.libs.db import Store
from inkcast.libs.db.backup import list_backups, take_backup

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


if __name__ == "__main__":
    unittest.main()
