# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""promote.yml can actually open its pull request (issue #80): it never did, on any run."""

import json
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"


class Promote(unittest.TestCase):
    def test_every_label_a_workflow_applies_is_one_it_creates(self):
        # `gh pr create --label X` fails outright when the label doesn't exist, and nothing else creates labels.
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text()
            for label in re.findall(r"--label\s+([\w-]+)(?=\s|$)", text):   # gh's bare names, not docker's "k=v"
                self.assertRegex(text, rf"gh label create {re.escape(label)} --force", f"{path.name}: label {label}")

    def test_the_actions_token_may_open_pull_requests_and_stays_read_only_by_default(self):
        settings = json.loads((REPO / ".github" / "repo-settings.json").read_text())
        self.assertEqual(settings["actions_workflow_permissions"],
                         {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True})
        script = (REPO / "scripts" / "repo" / "settings.py").read_text()
        self.assertIn('f"repos/{repo}/actions/permissions/workflow"', script)   # checked and applied

    def test_squash_merges_take_the_title_only(self):
        # PR bodies written from the automation workspace carry a tooling attribution line that can't be removed
        # there; PR_BODY would copy it into main on every merge (issue #142). The PR title is the commit.
        settings = json.loads((REPO / ".github" / "repo-settings.json").read_text())
        self.assertEqual(settings["repository"]["squash_merge_commit_title"], "PR_TITLE")
        self.assertEqual(settings["repository"]["squash_merge_commit_message"], "BLANK")

    def test_a_failure_to_open_the_pull_request_is_readable(self):
        text = (WORKFLOWS / "promote.yml").read_text()
        self.assertIn('scripts/ci-run.sh "open the promotion pull request" gh pr create', text)
        self.assertRegex(text, r"permissions:[\s\S]*issues: write")


if __name__ == "__main__":
    unittest.main()
