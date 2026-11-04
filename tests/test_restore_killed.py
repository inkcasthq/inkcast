# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A restore killed outright leaves its staging copy; the next backup removes it (issue #404, item 2).

A stop or a failure removes ``.inkcast.db.restoring`` (#128, #202); a kill (SIGKILL: the kubelet's, at the end of a
pod's grace period, or an OOM kill) runs no cleanup, and the copy, a whole backup with the erasure records carried into
it, stayed until the next restore overwrote it. Its name carries no time, so retention never saw it. Now ``take_backup``
removes it when no restore holds the data directory's lock, the one a running restore holds from before it stages
anything until after its swap and the kernel drops with a killed process. The restore here is a real ``inkcast restore``
process, stopped (SIGSTOP) by itself after the first step of its copy and then killed; nothing is slept for."""

import fcntl
import logging
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from inkcast.libs.db import Store
from inkcast.libs.db.backup import COPY_PAGES, take_backup

ROOT = pathlib.Path(__file__).resolve().parents[1]
REMOVED = "restore.staging.removed"

# `inkcast restore`, through the launcher as the Job runs it, whose copy stops itself (SIGSTOP) after its first step of
# COPY_PAGES pages: the staging copy then holds that step, and the test kills the process there.
_STOPS_MID_COPY = r"""
import os, signal, sys
from inkcast.libs.db import backup as module
from inkcast.launcher import main
backup, database = sys.argv[1], sys.argv[2]
real = module.sqlite3.connect

class Stepping:
    def __init__(self, conn):
        self._conn = conn

    def backup(self, target, *, pages=-1, progress=None, **kw):
        def stop_here(status, remaining, total):
            os.kill(os.getpid(), signal.SIGSTOP)
            progress(status, remaining, total)
        return self._conn.backup(target, pages=pages, progress=stop_here, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)

def connect(db, *a, **kw):
    conn = real(db, *a, **kw)
    return Stepping(conn) if str(db).startswith("file:") and "mode=ro" in str(db) else conn

module.sqlite3.connect = connect
main(["restore", backup, "--database", database, "--force"])
"""


class AKilledRestore(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="restore-killed-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.db = self.base / "data" / "inkcast.db"
        self.db.parent.mkdir()
        self.marker = "owner-r404-" + uuid.uuid4().hex
        store = Store(str(self.db), "k" * 40)
        person = store.upsert_user(f"local:{self.marker}", self.marker, None, None)
        store.close()
        ballast = sqlite3.connect(self.db)                     # more than one step of COPY_PAGES pages to copy
        ballast.execute("CREATE TABLE ballast (blob BLOB)")
        with ballast:
            ballast.executemany("INSERT INTO ballast VALUES (?)", [(os.urandom(4000),)] * (COPY_PAGES * 4))
        ballast.close()
        self.now = time.time()
        self.later = self.now + 60                                            # the backups below: each its own second
        self.backup = take_backup(self.db, self.base / "backups", now=self.now).path     # holds the person
        store = Store(str(self.db), "k" * 40)
        self.assertEqual(store.erase_user(person["id"])["users"], 1)         # the live database no longer does
        store.close()
        self.staging = self.db.with_name(".inkcast.db.restoring")
        self.assertEqual(self.holding(), [], "before the restore, no file on the data volume holds the person")

    def holding(self) -> list[str]:
        """Every file on the data volume whose bytes hold the person."""
        return sorted(p.name for p in self.db.parent.iterdir() if p.is_file() and self.marker.encode() in p.read_bytes())

    def kill_a_restore_mid_copy(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "INKCAST_LOG_FORMAT": "json"}
        restore = subprocess.Popen([sys.executable, "-c", _STOPS_MID_COPY, str(self.backup), str(self.db)], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _, status = os.waitpid(restore.pid, os.WUNTRACED)       # it stopped itself after the first step of its copy
        self.assertTrue(os.WIFSTOPPED(status), f"the restore ended before it could be killed mid-copy: {status}")
        os.kill(restore.pid, signal.SIGKILL)
        restore.wait()
        self.assertTrue(self.staging.exists(), "the kill left the staging copy")
        self.assertTrue(self.staging.with_name(self.staging.name + "-journal").exists(), "and its journal")
        self.assertIn(self.staging.name, self.holding(), "the copy holds the person the backup holds")

    def test_the_next_backup_removes_the_copy_and_its_journal(self):
        self.kill_a_restore_mid_copy()
        size = self.staging.stat().st_size + self.staging.with_name(self.staging.name + "-journal").stat().st_size
        with self.assertLogs("inkcast.libs.db.backup", logging.WARNING) as logs:
            take_backup(self.db, self.base / "backups", now=self.later)
        (removed,) = logs.records
        self.assertEqual((removed.event, removed.bytes), (REMOVED, size))
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), ["inkcast.db"])
        self.assertEqual(self.holding(), [], "no file on the data volume holds the person")

    def test_a_running_restore_s_copy_is_left_to_it(self):
        self.kill_a_restore_mid_copy()
        fd = os.open(self.db.parent, os.O_RDONLY)               # a restore in progress: the directory's lock held
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertNoLogs("inkcast.libs.db.backup", logging.WARNING):
            take_backup(self.db, self.base / "backups", now=self.later)
        self.assertTrue(self.staging.exists(), "a backup never takes a running restore's copy")
        fcntl.flock(fd, fcntl.LOCK_UN)                           # the restore is gone
        with self.assertLogs("inkcast.libs.db.backup", logging.WARNING):
            take_backup(self.db, self.base / "backups", now=self.later + 1)
        self.assertFalse(self.staging.exists())

    def test_a_directory_that_cannot_be_locked_leaves_the_copy_with_a_warning(self):
        self.kill_a_restore_mid_copy()
        with mock.patch("inkcast.libs.db.backup.fcntl.flock", side_effect=OSError("flock is not supported")), \
                self.assertLogs("inkcast.libs.db.backup", logging.WARNING) as logs:
            take_backup(self.db, self.base / "backups", now=self.later)
        (left,) = logs.records
        self.assertFalse(hasattr(left, "event"))
        self.assertIn("cannot be locked", left.getMessage())
        self.assertTrue(self.staging.exists())


if __name__ == "__main__":
    unittest.main()
