# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The backlog tracker: docs/backlog/order.yaml places every open story once, and TRACKER.md is generated from it."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backlog" / "tracker.py"

spec = importlib.util.spec_from_file_location("backlog_tracker", SCRIPT)
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)


def backlog(*stories: tuple[str, str]) -> list[dict]:
    return [{"id": "E1", "title": "Epic", "status": "planned", "goal": "g", "features": [
        {"id": "F1.1", "title": "Feature", "stories": [{"id": sid, "title": f"Story {sid}", "status": status}
                                                        for sid, status in stories]}]}]


def order(*items: str, tooling: tuple[str, ...] = (), issues: dict | None = None) -> dict:
    return {"waves": [{"id": "W1", "title": "Wave", "why": "Because.", "items": list(items)}],
            "tooling": list(tooling), "issues": issues or {}}


class ValidateTest(unittest.TestCase):
    def problems(self, epics: list[dict], plan: dict) -> list[str]:
        return tracker.validate(tracker.index(epics), plan)

    def test_valid_order_has_no_problems(self):
        epics = backlog(("F1.1.S1", "planned"), ("F1.1.S2", "done"), ("F1.1.S3", "in-progress"))
        self.assertEqual(self.problems(epics, order("F1.1.S3", "#7", "F1.1.S1", issues={7: "A bug"})), [])

    def test_open_story_without_a_place_fails(self):
        problems = self.problems(backlog(("F1.1.S1", "planned"), ("F1.1.S2", "in-progress")), order("F1.1.S1"))
        self.assertEqual(problems, ["F1.1.S2 is in-progress but has no place in docs/backlog/order.yaml"])

    def test_done_story_needs_no_place(self):
        self.assertEqual(self.problems(backlog(("F1.1.S1", "done")), order()), [])

    def test_an_item_placed_twice_fails_even_across_waves_and_tooling(self):
        plan = order("#7", tooling=("#7",), issues={7: "A bug"})
        self.assertIn("#7 is placed twice (W1 and tooling)", self.problems(backlog(), plan))

    def test_unknown_story_malformed_item_and_untitled_issue_fail(self):
        problems = self.problems(backlog(), order("F9.9.S9", "F1.1", "#8"))
        self.assertEqual(problems, ["F9.9.S9 (W1) is not in docs/backlog/E*.yaml",
                                    "'F1.1' (W1) is neither a story id nor '#N'",
                                    "#8 (W1) has no title under 'issues:'"])

    def test_unknown_status_fails(self):
        problems = self.problems(backlog(("F1.1.S1", "in progress")), order("F1.1.S1"))
        self.assertEqual(problems, ["F1.1.S1: status 'in progress' is not one of done, in-progress, planned"])


class RenderTest(unittest.TestCase):
    def test_order_numbers_run_across_waves_and_each_story_shows_its_place(self):
        epics = backlog(("F1.1.S1", "planned"), ("F1.1.S2", "done"))
        epics[0]["features"][0]["stories"][0]["issue"] = 12
        plan = {"waves": [{"id": "W1", "title": "One", "why": "x", "items": ["#7"]},
                          {"id": "W2", "title": "Two", "why": "y", "items": ["F1.1.S1"]}],
                "issues": {7: "A | bug"}}
        text = tracker.render(epics, plan)
        self.assertIn("| 1 | #7 | open issue | A \\| bug | | [#7](https://github.com/inkcasthq/inkcast/issues/7) |", text)
        self.assertIn("| 2 | F1.1.S1 | planned | Story F1.1.S1 | E1 "
                      "| [#12](https://github.com/inkcasthq/inkcast/issues/12) |", text)
        self.assertIn("| F1.1.S1 | planned | Story F1.1.S1 | [#12](https://github.com/inkcasthq/inkcast/issues/12) "
                      "| 2 (W2) |", text)
        self.assertIn("The backlog holds 2 stories: 1 done, 0 in progress and 1 planned.", text)


class RepositoryTest(unittest.TestCase):
    def test_the_repository_order_is_valid_and_the_tracker_is_current(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
