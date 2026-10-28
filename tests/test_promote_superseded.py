# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""promote.yml closes the promotion PRs a newer one supersedes (issue #298), and nothing else."""

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "superseded_promotions.py"
WORKFLOW = REPO / ".github" / "workflows" / "promote.yml"

_spec = importlib.util.spec_from_file_location("superseded_promotions", SCRIPT)
superseded_promotions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(superseded_promotions)

BOT = {"is_bot": True, "login": "app/github-actions"}
DEPLOY = [{"name": "deploy"}]
CURRENT = "promote/staging-a8053c3e8e77"


def pr(number, head, author=BOT, labels=DEPLOY):
    return {"number": number, "headRefName": head, "author": author, "labels": labels}


class Superseded(unittest.TestCase):
    def test_the_environments_older_bot_prs_are_closed_and_the_new_one_is_kept(self):
        prs = [pr(300, CURRENT), pr(294, "promote/staging-419ae335f730"), pr(243, "promote/staging-0123456789ab")]
        self.assertEqual(superseded_promotions.superseded(prs, "staging", CURRENT), [294, 243])

    def test_nothing_else_is_ever_closed(self):
        prs = [
            pr(1, "promote/production-419ae335f730"),                                   # another environment
            pr(2, "promote/staging-419ae335f730", author={"is_bot": False, "login": "on-call"}),   # a person's
            pr(3, "promote/staging-419ae335f730", labels=[{"name": "documentation"}]),   # not a deploy PR
            pr(4, "promote/staging-419ae335f730-fix"),                                   # not the bot's branch shape
            pr(5, "promote/staging-419AE335F730"),                                       # nor upper-case hex
            pr(6, "feature/promote/staging-419ae335f730"),
            pr(7, CURRENT),                                                              # the new PR itself
        ]
        self.assertEqual(superseded_promotions.superseded(prs, "staging", CURRENT), [])

    def test_production_closes_only_production(self):
        prs = [pr(10, "promote/production-419ae335f730"), pr(11, "promote/staging-419ae335f730")]
        self.assertEqual(superseded_promotions.superseded(prs, "production", "promote/production-a8053c3e8e77"), [10])

    def test_the_script_reads_gh_json_and_prints_one_number_per_line(self):
        prs = [pr(300, CURRENT), pr(294, "promote/staging-419ae335f730"), pr(243, "promote/staging-0123456789ab")]
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--environment", "staging", "--current", CURRENT],
            input=json.dumps(prs), capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(out, "294\n243\n")


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_runs_for_one_environment_never_overlap(self):
        # Two overlapping runs would each close the other's PR.
        self.assertIn("concurrency:\n  group: promote-${{ inputs.environment || 'staging' }}\n"
                      "  cancel-in-progress: false\n", self.text)

    def test_older_prs_are_closed_only_after_the_new_one_is_open(self):
        create = self.text.index('scripts/ci-run.sh "open the promotion pull request" gh pr create')
        close = self.text.index('python3 scripts/superseded_promotions.py --environment "$ENVIRONMENT" --current "$branch"')
        self.assertLess(create, close)
        # A re-run for the same digest leaves through "already open" first, closing nothing.
        self.assertLess(self.text.index("is already open"), close)
        self.assertIn('gh pr list --state open --label deploy --limit 200 --json number,headRefName,author,labels',
                      self.text)
        self.assertIn('gh pr close "$n" --delete-branch --comment "Superseded by #$new', self.text)


if __name__ == "__main__":
    unittest.main()
