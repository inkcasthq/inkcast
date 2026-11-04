# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Two restores stamped in the same second (issue #202).

The database a restore replaces is set aside as ``<db>.before-restore-<time>``, to the second. A second restore in
that second found the name taken at ``os.link``, after it had made its staging copy: a ``FileExistsError`` and a
full copy of the backup (``.inkcast.db.restoring``) left on the data volume, which nothing removed. Now the restore
refuses before it copies anything, with a sentence that says what to do, and the copy the first restore set aside
is left as it was: the same name, so it ages out with the backups as #402 promised."""

import pathlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from inkcast.libs.db import Store
from inkcast.libs.db.backup import BackupError, aside_copies, restore_backup, take_backup


def users(path: pathlib.Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


class ASecondRestoreInTheSameSecond(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="restore-same-second-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.db = self.base / "data" / "inkcast.db"
        self.db.parent.mkdir()
        store = Store(str(self.db), "k" * 40)
        store.upsert_user("local:owner", "owner", None, None)
        # The test's own clock, at noon UTC tomorrow: every name here is stamped from it, and the backup a day on below
        # must fall on the next date. On the wall clock, a run after 23:00 UTC put it two dates on, which #405 refuses.
        start = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
        self.backup = take_backup(self.db, self.base / "backups", now=start.timestamp()).path
        store.upsert_user("local:second", "second", None, None)
        store.close()
        self.now = start.timestamp() + 3600                    # an hour on: the heartbeat is stale
        self.aside = restore_backup(self.backup, self.db, now=self.now)
        self.assertEqual(users(self.db), 1)
        self.assertEqual(users(self.aside), 2)

    def test_is_refused_before_it_copies_anything(self):
        staged = []

        def saw_staging(event, args):
            if event == "sqlite3.connect" and str(args[0]).endswith(".restoring"):
                staged.append(str(args[0]))

        sys.addaudithook(saw_staging)
        with self.assertRaisesRegex(BackupError, "before-restore-.*already there.*second"):
            restore_backup(self.backup, self.db, now=self.now)
        self.assertEqual(staged, [], "the second restore made its staging copy before it was refused")
        left = sorted(p.name for p in self.db.parent.iterdir())
        self.assertEqual(left, ["inkcast.db", self.aside.name], left)
        self.assertEqual(users(self.db), 1)                     # the first restore's result stands
        self.assertEqual(users(self.aside), 2)                  # and the copy it set aside is as it was

    def test_the_next_second_restores_again_and_both_copies_age_out(self):
        later = restore_backup(self.backup, self.db, now=self.now + 1)
        self.assertNotEqual(later, self.aside)
        self.assertEqual([p.name for p in aside_copies(self.db)], [self.aside.name, later.name])
        # #402: a copy a restore sets aside ages out with the backups; the names this change produces are the same. A
        # day on, with keep=1: a longer gap is what #405 refuses to prune after.
        done = take_backup(self.db, self.base / "backups", keep=1, now=self.now + 86400)
        self.assertEqual(done.removed_copies, 2)
        self.assertEqual(aside_copies(self.db), [])


if __name__ == "__main__":
    unittest.main()
