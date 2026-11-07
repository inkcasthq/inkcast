# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #571: each delivery phase is one milestone, an issue's place in the order gives its milestone, and the forms and
the pull request template say so.

GitHub's issue forms have no milestone key, so ``tracker.py`` is what sets one. ``gh`` is faked by a small stand-in for the
repository, so each test reads what a real write would have changed."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backlog_tracker_571", ROOT / "scripts" / "backlog" / "tracker.py")
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)

MILESTONES = {p: {"title": f"Phase {p}", "description": f"Goal of {p}."} for p in tracker.PHASES}
FORMS = sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml"))
FORM_KEYS = {"name", "description", "title", "labels", "assignees", "projects", "type", "body"}  # GitHub's list, see the issue
FIELD_TYPES = {"markdown", "input", "textarea", "dropdown", "checkboxes", "upload"}
LABELS = {"bug", "story", "task"}  # each exists on the repository (`gh api repos/inkcasthq/inkcast/labels`, 2026-11-07)


def stories(*entries: tuple[str, dict]) -> dict:
    """Story id -> (epic, feature, story), each entry a story's fields, in a feature of phase 1."""
    feature = {"id": "F1.1", "phase": 1}
    return {sid: ({"id": "E1"}, feature, {"id": sid, "status": "planned", **fields}) for sid, fields in entries}


def order(*items: str, phase=1, tooling: tuple[str, ...] = (), tracking: tuple[str, ...] = ()) -> dict:
    return {"milestones": MILESTONES, "waves": [{"id": "W1", "phase": phase, "items": list(items)}],
            "tooling": list(tooling), "tracking": list(tracking)}


class FakeGitHub:
    """What ``gh`` reads and writes on a repository: the milestones, the issues and the open pull requests."""

    def __init__(self, milestones: list[dict] | None = None, issues: list[dict] | None = None, prs: list[dict] | None = None):
        self.milestones, self.issues, self.prs = list(milestones or []), list(issues or []), list(prs or [])
        self.writes: list[list[str]] = []

    def __call__(self, argv: list[str], **_) -> subprocess.CompletedProcess:
        assert argv[0] == "gh", argv
        if argv[1:3] == ["issue", "list"]:
            return self.answer(self.issues)
        if argv[1:3] == ["pr", "list"]:
            return self.answer(self.prs)
        if "-X" in argv:
            self.writes.append(argv[1:])
            return self.answer(self.write(argv))
        return self.answer([self.milestones])                                 # --paginate --slurp: a list of pages

    def write(self, argv: list[str]) -> dict:
        method, path = argv[argv.index("-X") + 1], argv[argv.index("-X") + 2]
        fields = dict(a.split("=", 1) for a in argv[argv.index("-X") + 3:] if "=" in a)
        if path.endswith("/milestones") and method == "POST":
            made = {"number": len(self.milestones) + 1, **fields}
            self.milestones.append(made)
            return made
        number = int(path.rsplit("/", 1)[1])
        if "/milestones/" in path:
            next(m for m in self.milestones if m["number"] == number).update(fields)
            return {}
        by_number = {m["number"]: m for m in self.milestones}
        milestone = None if fields["milestone"] == "null" else by_number[int(fields["milestone"])]
        for item in (*self.issues, *self.prs):
            if item["number"] == number:
                item["milestone"] = milestone
        return {}

    @staticmethod
    def answer(value) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(value))


class Applied(unittest.TestCase):
    def apply(self, github: FakeGitHub, known: dict, plan: dict) -> tuple[list[str], list[str]]:
        with mock.patch.object(tracker.subprocess, "run", github):
            return tracker.apply_milestones("o/r", known, plan)

    def drift(self, github: FakeGitHub, known: dict, plan: dict) -> list[str]:
        with mock.patch.object(tracker.subprocess, "run", github):
            return tracker.milestone_drift("o/r", known, plan)


class FindingEachPhaseIsAMilestone(Applied):
    def test_a_missing_milestone_is_reported_then_made_with_its_goal(self):
        github = FakeGitHub()
        self.assertIn("milestone 'Phase 1' does not exist", self.drift(github, {}, order()))
        self.assertEqual(github.writes, [], "--drift wrote")
        self.apply(github, {}, order())
        self.assertEqual(sorted(str(m["title"]) for m in github.milestones), sorted(f"Phase {p}" for p in tracker.PHASES))
        self.assertEqual(next(m for m in github.milestones if m["title"] == "Phase 1")["description"], "Goal of 1.")

    def test_a_changed_description_is_written_to_the_same_milestone(self):
        github = FakeGitHub(milestones=[{"number": 7, "title": "Phase 1", "description": "Old."}])
        self.assertIn("milestone 'Phase 1': its description differs from order.yaml", self.drift(github, {}, order()))
        self.apply(github, {}, order())
        self.assertEqual(github.milestones[0]["description"], "Goal of 1.")
        self.assertEqual(github.milestones[0]["number"], 7)

    def test_a_milestone_outside_the_order_is_left_for_a_person(self):
        github = FakeGitHub(milestones=[{"number": 9, "title": "Old name", "description": ""}])
        self.assertIn("milestone 'Old name' is on GitHub but not in order.yaml", self.drift(github, {}, order()))
        _, settle = self.apply(github, {}, order())
        self.assertEqual(settle, ["milestone 'Old name' is on GitHub but not in order.yaml"])
        self.assertIn(9, [m["number"] for m in github.milestones])

    def test_a_due_date_and_a_closed_state_are_not_touched(self):
        """The owner sets dates, and a milestone closes at its release: neither is a difference."""
        milestones = [{"number": i, "title": f"Phase {p}", "description": f"Goal of {p}.", "state": "closed",
                       "due_on": "2027-01-01T00:00:00Z"} for i, p in enumerate(tracker.PHASES)]
        github = FakeGitHub(milestones=milestones)
        self.assertEqual(self.drift(github, {}, order()), [])
        self.apply(github, {}, order())
        self.assertEqual(github.writes, [])


class FindingAnIssuesPlaceGivesItsMilestone(Applied):
    def everything(self) -> list[dict]:
        return [{"number": i, "title": f"Phase {p}", "description": f"Goal of {p}."} for i, p in enumerate(tracker.PHASES, 1)]

    def test_a_placed_issue_and_a_placed_stories_issue_take_their_waves_phase(self):
        known = stories(("F1.1.S1", {"issue": 10}))
        github = FakeGitHub(self.everything(), [{"number": 7, "milestone": None}, {"number": 10, "milestone": None}])
        plan = order("#7", "F1.1.S1", phase=2)
        self.assertEqual(self.drift(github, known, plan), ["#7: milestone None, expected 'Phase 2'",
                                                           "#10: milestone None, expected 'Phase 2'"])
        self.apply(github, known, plan)
        self.assertEqual([i["milestone"]["title"] for i in github.issues], ["Phase 2", "Phase 2"])

    def test_a_wrong_milestone_is_replaced(self):
        github = FakeGitHub(self.everything(), [{"number": 7, "milestone": {"title": "Phase 5"}}])
        self.apply(github, {}, order("#7"))
        self.assertEqual(github.issues[0]["milestone"]["title"], "Phase 1")

    def test_a_done_story_that_left_the_order_takes_its_own_phase(self):
        """So that a phase's percentage counts what is closed."""
        known = stories(("F1.1.S1", {"issue": 10, "status": "done", "phase": 3}))
        github = FakeGitHub(self.everything(), [{"number": 10, "milestone": None, "state": "CLOSED"}])
        self.apply(github, known, order())
        self.assertEqual(github.issues[0]["milestone"]["title"], "Phase 3")

    def test_a_done_story_with_no_phase_gets_none(self):
        known = {"F1.1.S1": ({"id": "E1"}, {"id": "F1.1"}, {"id": "F1.1.S1", "status": "done", "issue": 10})}
        github = FakeGitHub(self.everything(), [{"number": 10, "milestone": None}])
        self.assertEqual(self.drift(github, known, order()), [])

    def test_a_closed_issue_that_left_the_order_keeps_its_milestone(self):
        github = FakeGitHub(self.everything(), [{"number": 7, "milestone": {"title": "Phase 2"}, "state": "CLOSED"}])
        self.assertEqual(self.drift(github, {}, order()), [])
        self.apply(github, {}, order())
        self.assertEqual(github.writes, [])

    def test_an_issue_outside_the_releases_loses_a_milestone_it_was_given(self):
        github = FakeGitHub(self.everything(), [{"number": 7, "milestone": {"title": "Phase 2"}}])
        plan = order(tooling=("#7",))
        self.assertEqual(self.drift(github, {}, plan), ["#7: milestone 'Phase 2', but it is outside the releases"])
        self.apply(github, {}, plan)
        self.assertIsNone(github.issues[0]["milestone"])
        self.assertIn("milestone=null", github.writes[-1], "the clear must reach GitHub as JSON null, through -F")

    def test_a_second_run_writes_nothing(self):
        known = stories(("F1.1.S1", {"issue": 10}))
        github = FakeGitHub([], [{"number": 7, "milestone": None}, {"number": 10, "milestone": None}])
        plan = order("#7", "F1.1.S1")
        first, _ = self.apply(github, known, plan)
        self.assertEqual(len(first), len(tracker.PHASES) + 2)
        github.writes.clear()
        self.assertEqual(self.apply(github, known, plan), ([], []))
        self.assertEqual(github.writes, [])


class FindingAPullRequestTakesTheMilestoneOfTheIssueItCloses(Applied):
    def everything(self) -> list[dict]:
        return [{"number": i, "title": f"Phase {p}", "description": f"Goal of {p}."} for i, p in enumerate(tracker.PHASES, 1)]

    def test_a_pull_request_with_another_milestone_is_reported_and_set(self):
        github = FakeGitHub(self.everything(), [{"number": 7, "milestone": {"title": "Phase 1"}}],
                            [{"number": 8, "milestone": None, "closingIssuesReferences": [{"number": 7}]}])
        self.assertEqual(self.drift(github, {}, order("#7")),
                         ["pull request #8: milestone None, but it closes #7, which expects 'Phase 1'"])
        self.apply(github, {}, order("#7"))
        self.assertEqual(github.prs[0]["milestone"]["title"], "Phase 1")

    def test_a_pull_request_closing_two_phases_may_have_either(self):
        plan = {"milestones": MILESTONES, "waves": [{"id": "W1", "phase": 1, "items": ["#7"]},
                                                    {"id": "W2", "phase": 2, "items": ["#9"]}]}
        github = FakeGitHub(self.everything(), [{"number": 7, "milestone": {"title": "Phase 1"}},
                                                {"number": 9, "milestone": {"title": "Phase 2"}}],
                            [{"number": 8, "milestone": {"title": "Phase 2"},
                              "closingIssuesReferences": [{"number": 7}, {"number": 9}]}])
        self.assertEqual(self.drift(github, {}, plan), [])

    def test_a_promotion_or_a_dependency_bump_closes_nothing_and_needs_none(self):
        github = FakeGitHub(self.everything(), [], [{"number": 8, "milestone": None, "closingIssuesReferences": []}])
        self.assertEqual(self.drift(github, {}, order()), [])


class FindingAPhaseWithoutAMilestoneFailsTheCheck(unittest.TestCase):
    def problems(self, milestones: dict) -> list[str]:
        return tracker.validate({}, {"milestones": milestones, "waves": []})

    def test_a_phase_with_no_definition(self):
        partial = {p: m for p, m in MILESTONES.items() if p != 3}
        self.assertEqual(self.problems(partial), ["phase 3 has no milestone title or description under 'milestones:'"])

    def test_a_definition_with_no_goal(self):
        self.assertEqual(self.problems({**MILESTONES, 2: {"title": "Phase 2"}}),
                         ["phase 2 has no milestone description under 'milestones:'"])

    def test_a_definition_for_no_phase(self):
        self.assertEqual(self.problems({**MILESTONES, 9: {"title": "Nine", "description": "d"}}),
                         ["milestone 9 is not one of the phases 1, 2, 3, 4, 5, ongoing"])

    def test_two_phases_with_one_title(self):
        problems = self.problems({**MILESTONES, 2: {"title": "Phase 1", "description": "d"}})
        self.assertEqual(len(problems), 1)
        self.assertIn("milestone titles repeat", problems[0])

    def test_the_repositorys_order_defines_every_phase(self):
        plan = yaml.safe_load((ROOT / "docs" / "backlog" / "order.yaml").read_text())
        self.assertEqual(set(plan["milestones"]), set(tracker.PHASES))


class FindingTheIssueForms(unittest.TestCase):
    """GitHub rejects a form it cannot read and shows it as a plain issue, with no error in any check."""

    def test_there_is_a_form_for_a_bug_a_story_and_a_task(self):
        self.assertEqual([f.name for f in FORMS if f.name != "config.yml"], ["bug.yml", "story.yml", "task.yml"])

    def forms(self) -> list[tuple[str, dict]]:
        return [(f.name, yaml.safe_load(f.read_text())) for f in FORMS if f.name != "config.yml"]

    def test_each_form_holds_githubs_keys_only(self):
        for name, form in self.forms():
            with self.subTest(name):
                self.assertLessEqual(set(form), FORM_KEYS, "a key GitHub does not read, and `milestone` is one")
                self.assertTrue({"name", "description", "body"} <= set(form))
                self.assertLessEqual(set(form["labels"]), LABELS)

    def test_the_names_are_unique(self):
        names = [form["name"] for _, form in self.forms()]
        self.assertEqual(len(set(names)), len(names))

    def test_each_field_is_well_formed(self):
        for name, form in self.forms():
            ids = [field["id"] for field in form["body"] if field["type"] != "markdown"]
            self.assertEqual(len(set(ids)), len(ids), name)
            for field in form["body"]:
                with self.subTest(name, field=field.get("id", "markdown")):
                    self.assertIn(field["type"], FIELD_TYPES)
                    if field["type"] == "markdown":
                        self.assertTrue(field["attributes"]["value"].strip())
                    else:
                        self.assertTrue(field["attributes"]["label"].strip())
                    if field["type"] == "dropdown":
                        self.assertTrue(field["attributes"]["options"])

    def test_each_form_says_how_the_milestone_is_set(self):
        for name, form in self.forms():
            with self.subTest(name):
                self.assertIn("docs/backlog/order.yaml", form["body"][0]["attributes"]["value"])

    def test_the_story_forms_title_keeps_the_id_that_drift_matches(self):
        title = dict(self.forms())["story.yml"]["title"]
        self.assertTrue(title.startswith("[F"), title)

    def test_the_private_security_report_stays_offered(self):
        config = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text())
        self.assertIn("Security vulnerability", [link["name"] for link in config["contact_links"]])


class FindingThePullRequestTemplate(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github" / "pull_request_template.md").read_text()

    def headings(self) -> list[str]:
        return [line[3:] for line in self.text.splitlines() if line.startswith("## ")]

    def test_it_asks_for_the_milestone_first_and_lists_the_checks_in_the_order_they_happen(self):
        self.assertEqual(self.headings()[0], "Release milestone or hotfix")
        stages = ["Before review", "Before merge", "On staging", "On production"]
        self.assertEqual([h for h in self.headings() if h in stages], stages)

    def test_the_definition_of_done_and_the_contributor_section_stay(self):
        self.assertIn("Definition of done (docs/definition-of-done.md)", self.headings())
        self.assertEqual(self.headings()[-1], "Contributor")
        self.assertIn("I have signed the Inkcast CLA", self.text)

    def test_its_links_resolve(self):
        """A link in the template is relative to .github/, as the file sits there."""
        import re
        for target in re.findall(r"\]\((\.\./[^)]+)\)", self.text):
            with self.subTest(target):
                self.assertTrue((ROOT / ".github" / target).resolve().exists())


if __name__ == "__main__":
    unittest.main()
