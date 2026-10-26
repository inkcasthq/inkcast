# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #190 (issue #176): the race tests leave the process as they found it.

``tests/test_column_race.py`` forces the interleaving by patching ``sqlite3.connect``, and the module it patches is
the one every test shares. Its second test enters pod B's patch on B's thread while pod A's patch is still active,
then ends A's patch before B's: B's exit restores what it saw on entry, A's hooked ``connect``. From then on every
``sqlite3.connect`` in the process, in every later test file, returns a hooked connection whose hook (the finished
test's pod A) starts another pod-B thread on the first statement after a ``PRAGMA table_info``, and that thread
patches ``sqlite3.connect`` again from the background while other tests run."""

import sqlite3
import unittest

import test_column_race


class TheRaceTestsRestoreSqlite(unittest.TestCase):
    def run_case(self, name: str) -> None:
        before = sqlite3.connect
        self.addCleanup(setattr, sqlite3, "connect", before)   # whatever the case leaves, the suite gets back
        result = unittest.TestResult()
        test_column_race.TwoPodsAddingTheSameColumn(name).run(result)
        self.assertEqual(result.errors + result.failures, [], name)
        self.assertIs(sqlite3.connect, before, f"{name} left sqlite3.connect patched: {sqlite3.connect!r}")

    def test_both_read_the_table_before_either_acts_restores_connect(self):
        self.run_case("test_both_read_the_table_before_either_acts")


if __name__ == "__main__":
    unittest.main()
