# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The GitHub Action's metadata file is valid YAML and declares what the guide documents (#286).

A plain scalar with ``: `` in it (the ``comment`` input's description) made the whole file unparseable, and no test
loaded it, so nothing caught it."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class TheActionMetadata(unittest.TestCase):
    def test_it_parses_and_declares_the_documented_inputs(self):
        action = yaml.safe_load((ROOT / "action/action.yml").read_text(encoding="utf-8"))
        documented = {"url", "token", "paths", "platforms", "dry-run", "comment", "fail-on-stale"}
        self.assertLessEqual(documented, set(action["inputs"]))
        self.assertEqual({"results", "urls"}, set(action["outputs"]))
        self.assertEqual("composite", action["runs"]["using"])


if __name__ == "__main__":
    unittest.main()
