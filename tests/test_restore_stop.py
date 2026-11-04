# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A restore stops before its swap when asked to, or past its deadline, and says so (issue #128).

``inkcast`` is PID 1 in the image, and a PID 1 drops every signal it has no handler for: a SIGTERM to a restore
(the kubelet's, when its pod is deleted) was ignored, the restore ran on through the pod's grace period and could
finish its swap, and restore.sh then said "Nothing was replaced" about a database that was. Now ``restore_main``
handles SIGTERM and Ctrl-C by setting a flag, ``restore_backup`` reads it between two steps of its copy and once
more just before its swap, and ``--not-after`` gives the restore the script's own deadline. A stop before the swap
leaves the old database, removes the staging copy and logs ``restore.stopped``; one after the last check changes
nothing, and the restore logs ``restore.done``."""

import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from inkcast.libs.db import Store
from inkcast.libs.db.backup import COPY_PAGES, RestoreStopped, restore_backup, take_backup

ROOT = pathlib.Path(__file__).resolve().parents[1]


def users(path: pathlib.Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


class _Restore(unittest.TestCase):
    """A backup with one user, and a current database with two: a swap shows as one user at the database's path."""

    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="restore-stop-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.db = self.base / "data" / "inkcast.db"
        self.db.parent.mkdir()
        store = Store(str(self.db), "k" * 40)
        store.upsert_user("local:owner", "owner", None, None)
        self.backup = take_backup(self.db, self.base / "backups").path
        store.upsert_user("local:second", "second", None, None)
        store.close()

    def assert_untouched(self):
        """The old database at its path, nothing staged or set aside."""
        self.assertEqual(users(self.db), 2)
        left = sorted(p.name for p in self.db.parent.iterdir())
        self.assertEqual(left, ["inkcast.db"], left)


class AStopBeforeTheSwap(_Restore):
    def test_a_stop_already_requested_is_seen_at_the_first_step_of_the_copy(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(RestoreStopped, "stop was requested"):
            restore_backup(self.backup, self.db, force=True, stop=stop)
        self.assert_untouched()

    def test_a_stop_during_the_copy_ends_it_within_one_step(self):
        # Every step of the copy is COPY_PAGES pages; the stop is requested from the second step's callback, so the
        # copy must end at the third at the latest, and the staging copy must go with it.
        ballast = sqlite3.connect(self.db)
        ballast.execute("CREATE TABLE ballast (blob BLOB)")
        with ballast:
            ballast.executemany("INSERT INTO ballast VALUES (?)", [(os.urandom(4000),)] * (COPY_PAGES * 4))
        ballast.close()
        self.backup = take_backup(self.db, self.base / "backups", now=time.time() + 1).path
        stop = threading.Event()
        steps = []
        real_connect = sqlite3.connect

        class Stepping:
            """A connection whose backup counts its steps and requests the stop from the second."""

            def __init__(self, conn):
                self._conn = conn

            def backup(self, target, *, pages=-1, progress=None, **kw):
                def counted(status, remaining, total):
                    steps.append(remaining)
                    if len(steps) == 2:
                        stop.set()
                    progress(status, remaining, total)
                return self._conn.backup(target, pages=pages, progress=counted, **kw)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def connect(database, *a, **kw):
            conn = real_connect(database, *a, **kw)
            return Stepping(conn) if str(database).startswith("file:") and "mode=ro" in str(database) else conn

        from inkcast.libs.db import backup as module
        self.addCleanup(setattr, module.sqlite3, "connect", module.sqlite3.connect)
        module.sqlite3.connect = connect
        with self.assertRaisesRegex(RestoreStopped, "stop was requested"):
            restore_backup(self.backup, self.db, force=True, stop=stop)
        self.assertEqual(len(steps), 2, f"the copy went on after the stop was requested: {len(steps)} steps")
        self.assertGreater(steps[-1], 0, "the stop was requested too late to show: the copy was done")
        self.assertEqual(users(self.db), 2)
        self.assertFalse(self.db.with_name(".inkcast.db.restoring").exists(), "the staging copy was left behind")

    def test_a_deadline_already_passed_stops_the_restore_the_same_way(self):
        with self.assertRaisesRegex(RestoreStopped, "deadline"):
            restore_backup(self.backup, self.db, force=True, not_after=time.time() - 1)
        self.assert_untouched()

    def test_a_deadline_still_ahead_lets_the_restore_through(self):
        aside = restore_backup(self.backup, self.db, force=True, not_after=time.time() + 60)
        self.assertEqual(users(self.db), 1)
        self.assertTrue(aside.exists())

    def test_a_stop_after_the_last_check_changes_nothing(self):
        # The stop lands as the swap begins (the hard link is its first step): the swap goes through, as the kubelet's
        # SIGKILL after the grace period must not find a half-made one.
        stop = threading.Event()
        aside_names = []

        def on_link(event, args):
            if event == "os.link" and str(args[0]) == str(self.db):
                aside_names.append(str(args[1]))
                stop.set()

        sys.addaudithook(on_link)
        aside = restore_backup(self.backup, self.db, force=True, stop=stop)
        self.assertEqual(aside_names, [str(aside)])
        self.assertEqual(users(self.db), 1)
        self.assertEqual(users(aside), 2)


# Runs `inkcast restore` through the launcher, as the Job does, and sends itself SIGTERM at the step named: ``copy``
# (the staging copy's connection is opened: the copy is about to start) or ``swap`` (the hard link that begins it).
_SIGNALLED_AT = r"""
import os, pathlib, signal, sys
backup, database, at = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
from inkcast.launcher import main

def signal_self(event, args):
    if at == "copy" and event == "sqlite3.connect" and str(args[0]).endswith(".restoring"):
        os.kill(os.getpid(), signal.SIGTERM)
    if at == "swap" and event == "os.link" and str(args[0]) == str(database):
        os.kill(os.getpid(), signal.SIGTERM)

sys.addaudithook(signal_self)
main(["restore", backup, "--database", str(database), "--force"])
"""


class ARestoreCommandSignalled(_Restore):
    """The command, with a real SIGTERM. Without the handler the process died of it at once, leaving the staging
    copy (and, as PID 1, would have ignored it and swapped). With it, a SIGTERM before the swap ends in
    ``restore.stopped`` and exit 1 with the old database in place, and one during the swap in ``restore.done``."""

    def run_restore(self, at: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "INKCAST_LOG_FORMAT": "json"}
        return subprocess.run([sys.executable, "-c", _SIGNALLED_AT, str(self.backup), str(self.db), at], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_a_sigterm_before_the_swap_stops_the_restore_and_says_so(self):
        done = self.run_restore("copy")
        self.assertEqual(done.returncode, 1, f"stdout: {done.stdout}\nstderr: {done.stderr}")
        self.assertIn('"event": "restore.stopped"', done.stdout)
        self.assertIn("nothing was replaced", done.stdout)
        self.assert_untouched()

    def test_a_sigterm_during_the_swap_lets_it_finish_and_says_done(self):
        done = self.run_restore("swap")
        self.assertEqual(done.returncode, 0, f"stdout: {done.stdout}\nstderr: {done.stderr}")
        self.assertIn('"event": "restore.done"', done.stdout)
        self.assertEqual(users(self.db), 1)

    def test_a_deadline_passed_is_refused_by_the_command(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "INKCAST_LOG_FORMAT": "json"}
        done = subprocess.run([sys.executable, "-c", "from inkcast.launcher import main; import sys; main(sys.argv[1:])",
                               "restore", str(self.backup), "--database", str(self.db), "--force",
                               "--not-after", str(int(time.time()) - 1)], env=env, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('"event": "restore.stopped"', done.stdout)
        self.assert_untouched()

    def test_the_handlers_do_not_outlive_the_command(self):
        # A test runner or a shell calling restore_main in-process keeps its own SIGINT (KeyboardInterrupt) after.
        from inkcast.launcher import main
        before = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        with self.assertRaises(SystemExit):
            main(["restore", str(self.backup), "--database", str(self.db), "--force"])
        self.assertEqual((signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)), before)


if __name__ == "__main__":
    unittest.main()
