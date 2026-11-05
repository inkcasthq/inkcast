# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""promote.yml goes on when a promotion it supersedes is merged before the close (issue #517).

The step lists the open promotion PRs, then closes each one superseded_promotions.py names. A PR merged between the
two (run 41951986708: #515, two seconds after the list) made `gh pr close` fail, and `set -e` failed the run after
it had opened its own PR. The test runs the step's close loop, as promote.yml has it, with a stand-in `gh`."""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "promote.yml"

# `gh pr close` fails as GitHub does for a merged PR; `gh pr view` answers the state the test sets.
GH = """#!/bin/sh
echo "$*" >> "$GH_CALLS"
case "$1 $2" in
  "pr close") echo "X Pull request #$3 can't be closed because it was already merged" >&2; exit 1 ;;
  "pr view") echo "$PR_STATE" ;;
  *) echo "unexpected gh call: $*" >&2; exit 2 ;;
esac
"""


def close_loop() -> str:
    """The step's loop over the superseded PRs, from `while read -r n; do` to its `done`."""
    found = re.search(r"^( *)while read -r n; do\n.*?^\1done$", WORKFLOW.read_text(), re.M | re.S)
    assert found, "promote.yml has no close loop"
    return found.group(0)


class ASupersededPromotionMergedMeanwhile(unittest.TestCase):
    def close(self, state: str) -> subprocess.CompletedProcess:
        stubs = pathlib.Path(tempfile.mkdtemp(prefix="promote-close-"))
        self.addCleanup(shutil.rmtree, stubs, True)
        (stubs / "gh").write_text(GH)
        (stubs / "gh").chmod(0o755)
        env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GH_CALLS": str(stubs / "calls"),
               "PR_STATE": state, "new": "516", "ENVIRONMENT": "staging"}
        # As the step runs it: under set -euo pipefail, fed the numbers superseded_promotions.py printed.
        script = f"set -euo pipefail\nprintf '515\\n' |\n{close_loop()}\n"
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)

    def test_a_pr_merged_before_its_close_does_not_fail_the_run(self):
        done = self.close("MERGED")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_pr_closed_before_its_close_does_not_fail_the_run(self):
        done = self.close("CLOSED")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_pr_still_open_after_a_failed_close_fails_the_run(self):
        # Merging it later would roll the environment back to its older digest.
        done = self.close("OPEN")
        self.assertNotEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
