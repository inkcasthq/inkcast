# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The review of PR #452 (issue #410): components/openfga/migrate.sh against the pinned PostgreSQL and OpenFGA images,
with the harness of tests/test_review_openfga_migrate.py (its container, its database and its runner). Docker and both
images must already be present, as there."""

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


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """This file's tests only: Review410 inherits the harness's own, which run in their module."""
    suite = unittest.TestSuite()
    suite.addTests(Review410(name) for name in sorted(vars(Review410)) if name.startswith("test_"))
    return suite
