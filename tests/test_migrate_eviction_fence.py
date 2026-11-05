# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The eviction fence of components/openfga/migrate.sh (issue #494; docs/research/openfga-2026-10.md §7, option 2).

A grace is not a fence: a runner paused rather than dead runs on past it, and its statement in flight with it (in the
review of PR #453, the next runner held the lock while the paused runner's statement still ran in 29 of 33 samples).
So `openfga migrate` runs under a name of its own, and the runner that takes the lock ends every session of its role
and database with that name before it repairs anything, with pg_terminate_backend(pid, 5000): a role may end its own
sessions (signalfuncs.c:88-103 at REL_18_6). A URI's application_name= would rename those sessions, pgx's URI over the
environment (pgconn/config.go:428 at v5.11.0), so it is refused. The harness tests run with the harness of
tests/test_review_openfga_migrate.py (Docker and both pinned images); the last two drive the script against a psql
stand-in, as tests/test_migrate_session_ended.py does, for a backend the server cannot end in time."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from collections.abc import Callable

import test_review_openfga_migrate as harness
from test_migrate_session_ended import bash

BUILDING = "SELECT count(*) FROM pg_stat_progress_create_index"
# Every process of a runner that `docker exec ... bash /tmp/migrate.sh` started: that bash and its descendants (its
# psql, `openfga migrate`, the probe's sleep), the bash first so that it starts nothing new. NEEDLE and SIG travel in
# the environment, so this script's own command line never matches.
SIGNAL_RUNNER = r"""
pids=" "
for p in /proc/[0-9]*; do
  c="$(tr "\0" " " < "$p/cmdline" 2>/dev/null)"
  [[ "$c" == "$NEEDLE"* ]] && pids+="${p#/proc/} "
done
[ "$pids" != " " ] || { echo "no runner" >&2; exit 1; }
grown=1
while [ "$grown" = 1 ]; do
  grown=0
  for p in /proc/[0-9]*; do
    n="${p#/proc/}"
    [[ "$pids" == *" $n "* ]] && continue
    read -r _ _ _ parent _ < "$p/stat" 2>/dev/null || continue
    [[ "$pids" == *" $parent "* ]] && { pids+="$n "; grown=1; }
  done
done
kill "-$SIG" $pids
"""


class EvictionFence(harness.MigrateScript):
    container = f"{harness.MigrateScript.container}-494"

    def wait_for(self, condition: Callable[[], bool], seconds: float, message: str) -> None:
        """Poll CONDITION until it holds, failing with MESSAGE after SECONDS: a state waited for, never a fixed sleep."""
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > deadline:
                self.fail(message)
            time.sleep(0.25)

    def hold_snapshot(self) -> Callable[[], str]:
        """An open snapshot, as a large table keeps a build running: 006's CREATE INDEX CONCURRENTLY waits for it. The
        returned call ends it."""
        snapshot = subprocess.Popen(["docker", "exec", self.container, "psql", "-h", "/tmp", "-U", "postgres", "-d", "openfga",
                                     "-c", "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM goose_db_version;"
                                     " SELECT pg_sleep(120); COMMIT;"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = ("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
               " WHERE query LIKE '%pg_sleep(120)%' AND pid <> pg_backend_pid()")
        self.addCleanup(snapshot.wait, 60)
        self.addCleanup(self.sql, end)
        self.wait_for(lambda: self.sql("SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(120)%'"
                                       " AND state = 'active' AND pid <> pg_backend_pid()") == "1", 30, "no snapshot")
        return lambda: self.sql(end)

    def runner(self) -> subprocess.Popen:
        """migrate.sh in the background, as the init container runs it, killed and reaped at the test's end."""
        run = subprocess.Popen(self.migrate_command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(run.wait, 120)
        self.addCleanup(run.kill)
        return run

    def signal_runner(self, sig: str, check: bool = True) -> None:
        """SIG to every process of the runner in the container; with CHECK, a failure when there is none."""
        subprocess.run(["docker", "exec", "-e", "NEEDLE=bash /tmp/migrate.sh", "-e", f"SIG={sig}", self.container,
                       "bash", "-c", SIGNAL_RUNNER], check=check, capture_output=True, timeout=60)

    def build_006(self) -> Callable[[], str]:
        """A schema at version 5 with 006 to apply, its build held by an open snapshot; the call that ends it."""
        self.assertMigrated()
        self.sql("DELETE FROM goose_db_version WHERE version_id = 6")
        self.sql("DROP INDEX idx_user_lookup")
        return self.hold_snapshot()

    def test_the_lock_session_and_openfga_migrate_have_names_of_their_own(self):
        """psql's session, which holds the lock, keeps `openfga-migrate`; `openfga migrate`'s, here building 006, is
        `openfga-migrate-goose` (PGAPPNAME on its command alone: pgx maps it to application_name,
        pgconn/config.go:711 at v5.11.0), so the fence can tell them apart."""
        end_snapshot = self.build_006()
        run = self.runner()
        self.wait_for(lambda: self.sql(BUILDING) == "1", 30, "006's build never started")
        holder = self.sql("SELECT a.application_name FROM pg_locks l JOIN pg_stat_activity a USING (pid)"
                          " WHERE l.locktype = 'advisory' AND l.granted")
        builder = self.sql("SELECT a.application_name, a.usename, a.datname FROM pg_stat_progress_create_index p"
                           " JOIN pg_stat_activity a USING (pid)")
        end_snapshot()
        log, _ = run.communicate(timeout=120)
        self.assertEqual(holder, "openfga-migrate", "the lock session's name")
        self.assertEqual(builder, "openfga-migrate-goose|openfga|openfga", "openfga migrate's session: name, role, database")
        self.assertEqual(run.returncode, 0, log)
        self.assertNotIn("fence:", log, "a run without an earlier runner's session logs no fence line")

    def test_a_paused_runner_has_its_build_ended_and_the_next_runner_migrates(self):
        """The residual of #474: a runner paused (SIGSTOP, all of it) during 006 while its lock session dies alone, so
        that its probe never runs. Its CREATE INDEX CONCURRENTLY runs on (client_connection_check_interval finds a
        client gone, not one paused with its connection open), and on main the next runner, after its grace, met that
        build in its repair. With the fence the next runner ends it, while the snapshot it waits for is still held and
        its runner still paused; the repair drops the INVALID index it leaves and 006 builds it again. Thawed, the
        paused runner fails (its statement and its lock session are gone) and records nothing: one version-6 row."""
        end_snapshot = self.build_006()
        bystander = subprocess.Popen(["docker", "exec", "-e", "PGPASSWORD=" + harness.PASSWORD, "-e", "PGAPPNAME=bystander",
                                      self.container, "psql", "-h", "127.0.0.1", "-U", "openfga", "-d", "openfga", "-Atc",
                                      "SELECT pg_sleep(120)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(bystander.wait, 60)
        self.addCleanup(self.sql, "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = 'bystander'")
        paused = self.runner()
        self.addCleanup(self.signal_runner, "CONT", False)   # before the kill and the reap above: cleanups run last first
        self.wait_for(lambda: self.sql(BUILDING) == "1", 30, "006's build never started")
        build_pid = self.sql("SELECT pid FROM pg_stat_progress_create_index")
        self.signal_runner("STOP")
        # Its lock session dies alone (#409), a middlebox or pg_terminate_backend, while the runner is paused.
        self.assertEqual(self.sql("SELECT count(pg_terminate_backend(pid)) FROM pg_locks"
                                  " WHERE locktype = 'advisory' AND granted"), "1")
        self.assertEqual(self.sql("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'bystander'"), "1")
        following = self.runner()
        # On a failure, end the runners' sessions while their scripts still live to see it: a runner that gives up on
        # its own (60 s without an answer) leaves its psql to this container's PID 1, the postmaster, which takes
        # that psql's exit for a crash of its own and restarts, failing the tests after this one.
        self.addCleanup(self.sql, "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = 'openfga'")
        gone = f"SELECT count(*) FROM pg_stat_activity WHERE pid = {build_pid}"
        # On main, the next runner's repair (DROP INDEX CONCURRENTLY) waited on that build, which waits on the snapshot.
        self.wait_for(lambda: self.sql(gone) == "0", 45,
                      f"the paused runner's build (pid {build_pid}) still runs 45 s after the next runner started")
        self.assertEqual(self.sql("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'bystander'"), "1",
                         "a session of the role under another name is not fenced")
        end_snapshot()
        log, _ = following.communicate(timeout=120)
        self.assertEqual(following.returncode, 0, log)
        self.assertIn(f"fence: ended 1 session(s) of openfga migrate left by an earlier runner (pid {build_pid})", log)
        self.assertNotIn(harness.PASSWORD, log)
        self.assertLess(log.index("fence: ended"), log.index("repair: idx_user_lookup is INVALID"), "the fence before the repair")
        self.assertEqual(self.sql("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')"), "t")
        self.assertEqual(self.sql("SELECT count(*) FROM goose_db_version WHERE version_id = 6"), "1")
        self.signal_runner("CONT")
        thawed, _ = paused.communicate(timeout=120)
        self.assertNotEqual(paused.returncode, 0, thawed)
        self.assertEqual(self.sql("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')"), "t")
        self.assertEqual(self.sql("SELECT count(*) FROM goose_db_version WHERE version_id = 6"), "1")

    def test_application_name_in_the_query_is_refused_before_psql(self):
        """pgx takes the URI's settings over the environment's (pgconn/config.go:428, mergeSettings, at v5.11.0), so a
        URI's application_name= renames `openfga migrate`'s sessions (measured: `x`, its build left running by the
        fence's statement) and the fence would miss a paused runner's. It is refused before psql, as options= is, in
        every case and encoding of its key."""
        for query in ("application_name=x", "APPLICATION_NAME=openfga-migrate-goose", "applic%61tion_name=x",
                      "fallback_application_name=x"):
            with self.subTest(query=query):
                code, log = self.migrate(uri=f"postgres://openfga:{harness.PASSWORD}@127.0.0.1:5432/openfga?sslmode=disable&{query}",
                                         connect_timeout="1")
                self.assertEqual(code, 1, log)
                self.assertEqual(len(log.splitlines()), 1, log)
                self.assertIn("the datastore URI (the Secret openfga-db-app, key uri) is refused", log)
                self.assertIn("application_name=", log)
                self.assertNotIn(harness.PASSWORD, log)
                self.assertEqual(self.sql("SELECT to_regclass('goose_db_version') IS NULL"), "t")


# A psql stand-in for the session: the check before it (psql -c 'SELECT 1') answers; then each statement, read up to
# the script's `\echo __done__`, gets an answer by what it asks. The fence's statement finds one session of
# `openfga migrate`, pid 4242, which pg_terminate_backend did not end (false); RECHECK answers whether it is still there.
STAND_IN = r"""#!/bin/sh
for a in "$@"; do [ "$a" = -c ] && { echo 1; exit 0; }; done
sql=""
while IFS= read -r line; do
  case "$line" in
    '\q') exit 0 ;;
    '\echo __done__')
      case "$sql" in
        *pg_try_advisory_lock*|*pg_advisory_unlock*) echo t ;;
        *pg_terminate_backend*) echo '1|4242|4242' ;;
        *'pid IN'*) echo "$RECHECK" ;;
        *'count(*) FROM pg_stat_activity'*) echo 0 ;;
        *) echo 'answered' ;;
      esac
      echo __done__; sql="" ;;
    *) sql="$sql $line" ;;
  esac
done
"""


@unittest.skipIf(bash() is None, "no bash 4.3 or later on PATH")
class AFenceThatCannotEndASession(unittest.TestCase):
    def migrate(self, recheck: str, uri_query: str = "", service_env: bool = False,
                fence_timeout: bool = False) -> tuple[int, str, bool]:
        """The script against the stand-in: its exit code, its log, and whether `openfga migrate` ran."""
        stubs = pathlib.Path(tempfile.mkdtemp(prefix="migrate-fence-"))
        self.addCleanup(shutil.rmtree, stubs, True)
        ran = stubs / "openfga-ran"
        (stubs / "psql").write_text(STAND_IN)
        (stubs / "openfga").write_text(f"#!/bin/sh\ntouch '{ran}'\nexit 0\n")
        (stubs / "service.conf").write_text("[hidden]\napplication_name=out-of-fence\n")
        for stub in ("psql", "openfga"):
            (stubs / stub).chmod(0o755)
        env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "CONNECT_TIMEOUT": "5",
               "OPENFGA_BINARY": str(stubs / "openfga"), "RECHECK": recheck,
               "OPENFGA_DATASTORE_URI": f"postgres://openfga:not-a-secret@127.0.0.1:5432/openfga?sslmode=disable{uri_query}",
               "PGSERVICEFILE": str(stubs / "service.conf")}
        env.pop("PGSERVICE", None)
        if service_env:
            env["PGSERVICE"] = "hidden"
        if fence_timeout:
            (stubs / "env.sh").write_text(
                'printf() { [[ "${2-}" != *"pg_terminate_backend(pid, 5000)"* ]] || fence_read=1; builtin printf "$@"; }\n'
                'read() { if [[ "${fence_read-}" = 1 ]]; then for a in "$@"; do [[ "$a" != -t ]] || return 142; done; fi; builtin read "$@"; }\n')
            env["BASH_ENV"] = str(stubs / "env.sh")
        done = subprocess.run([bash(), str(harness.SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        return done.returncode, done.stdout + done.stderr, ran.exists()

    def test_a_session_that_does_not_end_in_time_fails_the_run_before_the_migration(self):
        """pg_terminate_backend(pid, 5000) returns false when the backend has not exited in 5 s (signalfuncs.c:217-223
        at REL_18_6). Still there when the runner looks again, it fails the run, naming its pid, before the repair and
        before `openfga migrate`: the kubelet starts the run again."""
        code, log, ran = self.migrate(recheck="4242")
        self.assertEqual(code, 1, log)
        self.assertIn("pid 4242, did not end within 5 s: this run fails", log)
        self.assertNotIn("not-a-secret", log)
        self.assertFalse(ran, "openfga migrate ran beside a session the fence could not end")

    def test_a_session_that_ended_on_its_own_before_the_signal_does_not_fail_the_run(self):
        """pg_terminate_backend also returns false, with a warning, for a pid that is no backend any more
        (signalfuncs.c:67-76 at REL_18_6): a session that ended on its own between the fence's read of
        pg_stat_activity and its signal. Gone when the runner looks again, it is no failure."""
        code, log, ran = self.migrate(recheck="")
        self.assertEqual(code, 0, log)
        self.assertNotIn("did not end", log)
        self.assertIn("fence: ended 1 session(s) of openfga migrate left by an earlier runner (pid 4242)", log)
        self.assertTrue(ran, "openfga migrate did not run")

    def test_a_service_cannot_rename_the_migration_session(self):
        for kwargs in ({"uri_query": "&service=hidden"}, {"uri_query": "&%73ervice=hidden"},
                       {"uri_query": "&SERVICE=hidden"}, {"service_env": True}):
            with self.subTest(kwargs=kwargs):
                code, log, ran = self.migrate(recheck="", **kwargs)
                self.assertEqual(code, 1, log)
                self.assertIn("service", log)
                self.assertIn("is refused", log)
                self.assertFalse(ran)

    def test_a_fence_timeout_names_the_fence(self):
        code, log, ran = self.migrate(recheck="", fence_timeout=True)
        self.assertEqual(code, 1, log)
        self.assertIn("fence:", log)
        self.assertIn("no answer from the database session in 60 s", log)
        self.assertFalse(ran)


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """This file's tests only: EvictionFence inherits the harness's own, which run in their module."""
    suite = unittest.TestSuite()
    for case in (EvictionFence, AFenceThatCannotEndASession):
        suite.addTests(case(name) for name in sorted(vars(case)) if name.startswith("test_"))
    return suite
