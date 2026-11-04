# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The review of PR #453 (issue #409): components/openfga/migrate.sh against the pinned PostgreSQL and OpenFGA images,
with the harness of tests/test_review_openfga_migrate.py (its container, its database and its runner). Docker and both
images must already be present, as there."""

from __future__ import annotations

import subprocess
import unittest

import test_review_openfga_migrate as harness


class Review409(harness.MigrateScript):
    container = f"{harness.MigrateScript.container}-4xx-r"

    def migrate_with(self, **env: str) -> tuple[int, str]:
        """migrate.sh as the init container runs it, with ENV added to its environment: its exit code and its log."""
        command = self.migrate_command()
        command[2:2] = [f"-e{name}={value}" for name, value in env.items()]   # after `docker exec`
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


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """This file's tests only: Review409 inherits the harness's own, which run in their module."""
    suite = unittest.TestSuite()
    suite.addTests(Review409(name) for name in sorted(vars(Review409)) if name.startswith("test_"))
    return suite
