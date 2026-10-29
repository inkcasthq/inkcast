# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Every open feature in the backlog sits in a delivery phase (docs/epics/README.md, "Delivery phases")."""

import pathlib
import unittest

import yaml

BACKLOG = pathlib.Path(__file__).resolve().parents[1] / "docs" / "backlog"
PHASES = {1, 2, 3, 4, 5, "ongoing"}


def features():
    for path in sorted(BACKLOG.glob("E*.yaml")):
        for feature in yaml.safe_load(path.read_text()).get("features", []):
            yield path.name, feature


class EveryOpenFeatureHasAPhase(unittest.TestCase):
    def test_open_features_name_a_known_phase(self):
        for name, feature in features():
            if all(s.get("status") == "done" for s in feature["stories"]):
                continue
            with self.subTest(feature=feature["id"], file=name):
                self.assertIn(feature.get("phase"), PHASES, "add `phase:` (1-5 or ongoing) to the feature")

    def test_a_story_phase_is_known_and_differs_from_its_feature(self):
        for name, feature in features():
            for story in feature["stories"]:
                if "phase" not in story:
                    continue
                with self.subTest(story=story["id"], file=name):
                    self.assertIn(story["phase"], PHASES)
                    self.assertNotEqual(story["phase"], feature.get("phase"), "a story names a phase only to differ")


if __name__ == "__main__":
    unittest.main()
