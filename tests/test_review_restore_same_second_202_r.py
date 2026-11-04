# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #431 (issue #202): two restores in the same second, run at once.

The PR checks, before the copy, that the name the swap will give the current database is free. Two restores started
in the same second (two terminals, by hand) both pass that check, as neither has set its copy aside yet, and then
share the staging file ``.inkcast.db.restoring``: the second failed after its copy, with ``FileExistsError`` at
``os.link`` or with SQLite's own error as the first renamed the staging file away from it ("attempt to write a
readonly database", "unable to open database file"). Measured with two processes started at the same instant, four
runs: four of four ended so, none with the PR's message. ``sqlite3.Error`` is not a ``BackupError``: ``inkcast
restore`` logged no outcome for it, only a traceback.

The fix holds a lock on the data directory for the whole restore, taken before the PR's check: the second restore
is refused before it copies anything, with a sentence that says what to do, whichever of the two checks sees it."""

import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from inkcast.libs.db import Store
from inkcast.libs.db.backup import take_backup

ROOT = pathlib.Path(__file__).resolve().parents[1]

# One restore, started at the given instant; prints what became of it.
_RESTORE_AT = r"""
import pathlib, sys, time
from inkcast.libs.db.backup import BackupError, restore_backup
backup, db, at = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), float(sys.argv[3])
time.sleep(max(0.0, at - time.time()))
try:
    aside = restore_backup(backup, db, force=True)
    print("done", aside.name)
except BackupError as exc:
    print("refused", exc)
except BaseException as exc:
    print("raised", type(exc).__name__, exc)
"""


def users(path: pathlib.Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


class TwoRestoresAtOnceInTheSameSecond(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="review-restore-at-once-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.db = self.base / "data" / "inkcast.db"
        self.db.parent.mkdir()
        store = Store(str(self.db), "k" * 40)
        store.upsert_user("local:owner", "owner", None, None)
        store.close()
        conn = sqlite3.connect(self.db)                         # 20 MB: each copy takes long enough to overlap
        conn.execute("CREATE TABLE ballast (b BLOB)")
        conn.executemany("INSERT INTO ballast VALUES (randomblob(1048576))", [()] * 20)
        conn.commit()
        conn.close()
        self.backup = take_backup(self.db, self.base / "backups").path
        store = Store(str(self.db), "k" * 40)
        store.upsert_user("local:second", "second", None, None)
        store.close()

    def test_the_second_is_refused_before_it_copies_and_the_first_restores(self):
        at = int(time.time()) + 1.05                            # both early in the same second
        env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"}
        procs = [subprocess.Popen([sys.executable, "-c", _RESTORE_AT, str(self.backup), str(self.db), str(at)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                 for _ in range(2)]
        outcomes = sorted(p.communicate(timeout=120)[0].strip() for p in procs)
        self.assertEqual([o.split()[0] for o in outcomes], ["done", "refused"], outcomes)
        self.assertRegex(outcomes[1], "already there|Another restore", outcomes)
        aside = outcomes[0].split()[1]
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), ["inkcast.db", aside])
        self.assertEqual(users(self.db), 1)                     # the backup's one user
        self.assertEqual(users(self.db.with_name(aside)), 2)    # the database it replaced, whole


if __name__ == "__main__":
    unittest.main()
