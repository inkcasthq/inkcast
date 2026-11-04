# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The review of PRs #452 (issue #410) and #453 (issue #409): components/openfga/migrate.sh against the pinned
PostgreSQL and OpenFGA images, with the harness of tests/test_review_openfga_migrate.py (its container, its database
and its runner). Docker and both images must already be present, as there."""

from __future__ import annotations

import re
import subprocess
import time
import unittest

import test_review_openfga_migrate as harness


class Review410(harness.MigrateScript):
    container = f"{harness.MigrateScript.container}-4xx-r"

    def test_a_raw_question_mark_or_hash_in_the_password_reaches_no_log_and_no_command_line(self):
        """libpq ends a URI's userinfo at its first `@` before any `/` (fe-connect.c conninfo_uri_parse_options at
        REL_18_6), and pgx does the same (pgconn/parse_url.go at v5.11.0), so a raw `?` or `#` in the password is
        part of it: psql connects and the server serves. Go's net/url ends the authority there instead and prints
        the URI ("invalid port ... after host"). migrate.sh's split must take what libpq takes, or psql gets the
        whole URI on its command line and `openfga migrate` prints the password, all of it before a `#`."""
        for mark in ("?", "#"):
            password = f"Pw-410-review{mark}not-a-secret"
            with self.subTest(password=password.replace("not-a-secret", "...")):
                self.setUp()
                self.sql(f"ALTER ROLE openfga PASSWORD '{password}'", "postgres")
                self.addCleanup(self.sql, f"ALTER ROLE openfga PASSWORD '{harness.PASSWORD}'", "postgres")
                uri = f"postgres://openfga:{password}@127.0.0.1:5432/openfga?sslmode=disable"
                key = re.search(r'lock_key="\$\{LOCK_KEY:-(\d+)\}"', harness.SCRIPT.read_text())[1]
                holder = subprocess.Popen(["docker", "exec", self.container, "psql", "-h", "/tmp", "-U", "postgres", "-d", "openfga",
                                           "-Atc", f"SELECT pg_advisory_lock({key}), pg_sleep(3)"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.addCleanup(holder.wait, 60)
                runner = subprocess.Popen(self.migrate_command(uri=uri), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                session = []
                for _ in range(40):   # the session psql, while the runner waits for the lock the holder keeps
                    session = self.command_lines_holding("psql -X -q -At")
                    if session:
                        break
                    time.sleep(0.25)
                leaked = self.command_lines_holding("not-a-secret")
                log, _ = runner.communicate(timeout=120)
                self.assertTrue(session, "the runner's psql session was never seen")
                self.assertEqual(leaked, [], "the password in a command line")
                self.assertNotIn("Pw-410-review", log, "the password in the init container's log")
                self.assertNotIn("not-a-secret", log, "the password in the init container's log")
                self.assertEqual(runner.returncode, 0, log)
                self.assertEqual(self.sql("SELECT max(version_id) FROM goose_db_version"), "6")


class Review409(harness.MigrateScript):
    container = f"{harness.MigrateScript.container}-4xx-r-409"

    def migrate_with(self, **env: str) -> tuple[int, str]:
        """migrate.sh as the init container runs it, with ENV added to its environment: its exit code and its log."""
        command = self.migrate_command()
        command[-3:-3] = [f"-e{name}={value}" for name, value in env.items()]   # after the defaults, which they override
        done = subprocess.run(command, capture_output=True, text=True, timeout=300)
        return done.returncode, done.stdout + done.stderr

    def test_a_probe_interval_outside_whole_seconds_from_1_to_4_is_refused_before_the_database(self):
        """PROBE_SECONDS paces the probe and sizes the hand-over grace (2 x PROBE_SECONDS + 2). 0, a negative or a word
        made `while sleep ...; kill -0 ...` a busy loop (sleep fails at once, the loop tests only kill -0): about
        2,000 probes a second, measured; a fraction or a unit, which sleep takes, broke the grace's arithmetic, and
        bash skipped the grace with its `if` (measured: "0.5: syntax error: invalid arithmetic operator", exit 0).
        And at half pace the grace is the time a runner whose lock died still migrates: above 4 s it passes the 10 s
        of the issue. So the script refuses such a value at start-up, naming the variable, before it connects."""
        for value in ("0", "-1", "abc", "0.5", "2s", "5"):
            with self.subTest(value=value):
                code, log = self.migrate_with(PROBE_SECONDS=value)
                self.assertEqual(code, 1, log)
                self.assertIn("PROBE_SECONDS", log)
                self.assertNotIn("database reachable", log)
                self.assertEqual(self.sql("SELECT to_regclass('goose_db_version') IS NULL"), "t")

    def test_a_run_ends_when_its_migration_does_not_a_probe_interval_later(self):
        """The normal path, unchanged: the probe loop slept its interval before it looked at `openfga migrate`, so
        every run, a schema already current included, ended up to PROBE_SECONDS after its migration (measured with
        the default 2 s: 2.08 s a run against 0.10 s before #453, over 30 runs), and with `strategy: Recreate` that
        is downtime at every rollout. The run ends with the migration, with OpenFGA's own exit code."""
        self.assertMigrated()
        started = time.monotonic()
        code, log = self.migrate_with(PROBE_SECONDS="4")
        took = time.monotonic() - started
        self.assertEqual(code, 0, log)
        self.assertIn('"current version":6', log)
        self.assertLess(took, 2, f"a run on a current schema took {took:.1f} s with PROBE_SECONDS=4")
        code, log = self.migrate_with(PROBE_SECONDS="4", OPENFGA_DATASTORE_ENGINE="review-409")
        self.assertEqual(code, 1, log)
        self.assertIn("unknown datastore engine type: review-409", log)
        self.assertIn("openfga migrate exited 1: this run fails", log)


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """This file's tests only: its classes inherit the harness's own, which run in their module."""
    suite = unittest.TestSuite()
    for case in (Review410, Review409):
        suite.addTests(case(name) for name in sorted(vars(case)) if name.startswith("test_"))
    return suite
