# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The backlog tracker: docs/backlog/order.yaml places every open story once, and TRACKER.md is generated from it."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backlog" / "tracker.py"

spec = importlib.util.spec_from_file_location("backlog_tracker", SCRIPT)
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)


def backlog(*stories: tuple[str, str]) -> list[dict]:
    return [{"id": "E1", "title": "Epic", "status": "planned", "goal": "g", "features": [
        {"id": "F1.1", "title": "Feature", "phase": 1, "stories": [{"id": sid, "title": f"Story {sid}", "status": status}
                                                        for sid, status in stories]}]}]


def order(*items: str, tooling: tuple[str, ...] = (), issues: dict | None = None) -> dict:
    return {"waves": [{"id": "W1", "phase": 1, "title": "Wave", "why": "Because.", "items": list(items)}],
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
        plan = {"waves": [{"id": "W1", "phase": 1, "title": "One", "why": "x", "items": ["#7"]},
                          {"id": "W2", "phase": 1, "title": "Two", "why": "y", "items": ["F1.1.S1"]}],
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


class ReviewFindingsTest(unittest.TestCase):
    """Inputs validate() used to accept, or crash on, and drift() cases it used to miss (OB3 review of 854b268)."""

    def problems(self, epics: list[dict], plan: dict) -> list[str]:
        return tracker.validate(tracker.index(epics), plan)

    def test_a_story_id_written_twice_is_refused(self):
        epics = backlog(("F1.1.S1", "planned")) + [{"id": "E2", "title": "E", "status": "planned", "goal": "g",
                                                    "features": [{"id": "F2.1", "title": "F", "stories": [
                                                        {"id": "F1.1.S1", "title": "Other", "status": "planned"}]}]}]
        with self.assertRaisesRegex(ValueError, r"F1\.1\.S1 is in docs/backlog twice \(E1 and E2\)"):
            tracker.index(epics)

    def test_a_story_under_tooling_is_refused_and_never_reaches_render(self):
        plan = order("F1.1.S1", tooling=("F1.1.S2",))
        problems = self.problems(backlog(("F1.1.S1", "planned"), ("F1.1.S2", "planned")), plan)
        self.assertIn("'F1.1.S2' (tooling) is not '#N': tooling holds open issues only", problems)

    def test_malformed_yaml_items_are_problems_not_tracebacks(self):
        unquoted = yaml.safe_load("waves:\n- id: W1\n  phase: 1\n  title: W\n  why: y\n  items:\n  - #342\n")
        self.assertIn("None (W1) is neither a story id nor '#N' (quote '#N' in YAML)",
                      self.problems(backlog(), unquoted))
        empty = yaml.safe_load("waves:\n- id: W1\n  phase: 1\n  title: W\n  why: y\n  items:\nissues:\n")
        self.assertEqual(self.problems(backlog(), empty), [])
        self.assertIn("### W1: W (phase 1, 0 of 0 stories done)", tracker.render(backlog(), empty))
        self.assertIn("342 (W1) is neither a story id nor '#N' (quote '#N' in YAML)",
                      self.problems(backlog(), order(342)))

    def test_the_tracking_issue_is_named_on_the_page_and_holds_issues_only(self):
        plan = {**order(issues={360: "Backlog tracker"}), "tracking": ["#360"]}
        self.assertEqual(self.problems(backlog(), plan), [])
        self.assertIn("Tracked in [#360](https://github.com/inkcasthq/inkcast/issues/360): Backlog tracker.",
                      tracker.render(backlog(), plan))
        plan["tracking"] = ["F1.1.S1"]
        self.assertIn("'F1.1.S1' (tracking) is not '#N': tracking holds open issues only",
                      self.problems(backlog(("F1.1.S1", "planned")), {**plan, "waves": [{**plan["waves"][0], "items": ["F1.1.S1"]}]}))

    def test_drift_counts_the_tracking_issue_as_placed(self):
        found = self.drift([{"number": 360, "title": "Backlog tracker", "state": "OPEN"}], backlog(),
                           {**order(issues={360: "Backlog tracker"}), "tracking": ["#360"]})
        self.assertEqual(found, [])

    def test_the_wave_header_counts_stories_only(self):
        plan = order("F1.1.S1", "#7", "#8", issues={7: "a", 8: "b"})
        self.assertIn("### W1: Wave (phase 1, 1 of 1 stories done)", tracker.render(backlog(("F1.1.S1", "done")), plan))

    def drift(self, gh_issues: list[dict], epics: list[dict], plan: dict) -> list[str]:
        answer = mock.Mock(stdout=json.dumps(gh_issues))
        with mock.patch.object(tracker.subprocess, "run", return_value=answer):
            return tracker.drift("o/r", tracker.index(epics), plan)

    def story_with_issue(self) -> list[dict]:
        epics = backlog(("F1.1.S1", "planned"))
        epics[0]["features"][0]["stories"][0]["issue"] = 10
        return epics

    def test_drift_compares_the_issue_titles_order_yaml_keeps(self):
        found = self.drift([{"number": 7, "title": "Renamed on GitHub", "state": "OPEN"},
                            {"number": 10, "title": "[F1.1.S1] Story F1.1.S1", "state": "OPEN"}],
                           self.story_with_issue(), order("F1.1.S1", "#7", issues={7: "Old title"}))
        self.assertEqual(found, ["#7: order.yaml's title 'Old title' is not GitHub's 'Renamed on GitHub'"])

    def test_drift_names_a_story_filed_twice_and_trusts_the_backlogs_issue(self):
        found = self.drift([{"number": 10, "title": "[F1.1.S1] Story F1.1.S1", "state": "OPEN"},
                            {"number": 11, "title": "[F1.1.S1] Story F1.1.S1 again", "state": "OPEN"}],
                           self.story_with_issue(), order("F1.1.S1"))
        self.assertEqual(found, ["issues #10 and #11 are both titled [F1.1.S1]",
                                 "#11 is open but has no place in docs/backlog/order.yaml: [F1.1.S1] Story F1.1.S1 again"])

    def test_drift_knows_a_storys_issue_without_the_id_in_its_title(self):
        found = self.drift([{"number": 10, "title": "Story F1.1.S1", "state": "OPEN"}],
                           self.story_with_issue(), order("F1.1.S1"))
        self.assertEqual(found, [])

    def test_drift_says_a_placed_issue_does_not_exist(self):
        found = self.drift([], backlog(), order("#7", issues={7: "a"}))
        self.assertEqual(found, ["#7 is in docs/backlog/order.yaml but does not exist"])


class PhaseTest(unittest.TestCase):
    """Each wave names its delivery phase, the phases run in order, and a story sits in a wave of its own phase."""

    def problems(self, epics: list[dict], waves: list[dict]) -> list[str]:
        return tracker.validate(tracker.index(epics), {"waves": waves, "issues": {}})

    def wave(self, wid: str, phase, *items: str) -> dict:
        return {"id": wid, "phase": phase, "title": "Wave", "why": "Because.", "items": list(items)}

    def test_a_wave_without_a_known_phase_fails(self):
        problems = self.problems(backlog(), [self.wave("W1", None), self.wave("W2", 6)])
        self.assertEqual(problems, ["wave 'W1': phase None is not one of 1, 2, 3, 4, 5, ongoing",
                                    "wave 'W2': phase 6 is not one of 1, 2, 3, 4, 5, ongoing"])

    def test_numbered_phases_never_go_back_and_ongoing_waves_sit_anywhere(self):
        waves = [self.wave("W1", 2), self.wave("W2", "ongoing"), self.wave("W3", 1), self.wave("W4", 3)]
        self.assertEqual(self.problems(backlog(), waves), ["wave 'W3' is phase 1, after 'W1' in phase 2"])

    def test_a_story_sits_in_a_wave_of_its_own_phase(self):
        epics = backlog(("F1.1.S1", "planned"), ("F1.1.S2", "planned"))
        epics[0]["features"][0]["stories"][1]["phase"] = 2       # differs from the feature's phase 1
        problems = self.problems(epics, [self.wave("W1", 1, "F1.1.S1", "F1.1.S2")])
        self.assertEqual(problems, ["F1.1.S2 (W1) is phase 2, but W1 is phase 1"])
        self.assertEqual(self.problems(epics, [self.wave("W1", 1, "F1.1.S1"), self.wave("W2", 2, "F1.1.S2")]), [])

    def test_ongoing_stories_sit_in_any_wave_but_an_ongoing_wave_holds_only_them(self):
        epics = backlog(("F1.1.S1", "planned"), ("F1.1.S2", "planned"))
        epics[0]["features"][0]["stories"][0]["phase"] = "ongoing"
        self.assertEqual(self.problems(epics, [self.wave("W1", 1, "F1.1.S1", "F1.1.S2")]), [])
        problems = self.problems(epics, [self.wave("W1", "ongoing", "F1.1.S1", "F1.1.S2")])
        self.assertEqual(problems, ["F1.1.S2 (W1) is phase 1, but W1 is phase 'ongoing'"])

    def test_the_wave_header_names_its_phase(self):
        text = tracker.render(backlog(("F1.1.S1", "planned")), {"waves": [self.wave("W9", "ongoing", "F1.1.S1")]})
        self.assertIn("### W9: Wave (phase ongoing, 0 of 1 stories done)", text)


class FileScriptTest(unittest.TestCase):
    def test_a_story_that_records_its_issue_is_never_filed_again(self):
        spec = importlib.util.spec_from_file_location("backlog_file", ROOT / "scripts" / "backlog" / "file.py")
        file_script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(file_script)
        epics = backlog(("F1.1.S1", "planned"), ("F1.1.S2", "planned"))
        epics[0]["features"][0]["stories"][0]["issue"] = 61   # filed under an older title
        out = io.StringIO()
        with mock.patch.object(file_script, "load", return_value=epics), contextlib.redirect_stdout(out):
            file_script.main(["o/r", "--dry-run"])
        self.assertNotIn("+ [F1.1.S1]", out.getvalue())
        self.assertIn("+ [F1.1.S2] Story F1.1.S2", out.getvalue())


if __name__ == "__main__":
    unittest.main()
