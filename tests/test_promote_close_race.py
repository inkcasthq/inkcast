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

# `gh pr close` fails as GitHub does for a merged PR, unless CLOSE_OK is set; `gh pr view` answers the state the test
# sets for that PR, fails as on a network error when the state is ERROR, and prints MERGED yet fails when it is
# MERGED_THEN_ERROR (a failed command's output is not an answer).
GH = """#!/bin/sh
echo "$*" >> "$GH_CALLS"
case "$1 $2" in
  "pr close") [ -z "$CLOSE_OK" ] || exit 0
    echo "X Pull request #$3 can't be closed because it was already merged" >&2; exit 1 ;;
  "pr view") eval "state=\\$PR_STATE_$3"
    [ "$state" != ERROR ] || { echo "HTTP 502: Bad Gateway" >&2; exit 1; }
    [ "$state" != MERGED_THEN_ERROR ] || { echo MERGED; echo "HTTP 502: Bad Gateway" >&2; exit 1; }
    echo "$state" ;;
  *) echo "unexpected gh call: $*" >&2; exit 2 ;;
esac
"""


def close_loop() -> str:
    """The step's loop over the superseded PRs, from `while read -r n; do` to its `done`."""
    found = [m.group(0) for m in re.finditer(r"^( *)while read -r n; do\n.*?^\1done$", WORKFLOW.read_text(), re.M | re.S)]
    assert len(found) == 1, f"promote.yml has {len(found)} loops over `read -r n`, not one: the test can't tell which"
    assert 'gh pr close "$n"' in found[0], "promote.yml's loop over `read -r n` no longer closes the superseded PRs"
    return found[0]


class ASupersededPromotionMergedMeanwhile(unittest.TestCase):
    def close(self, *states: str, close_ok: bool = False) -> subprocess.CompletedProcess:
        """Runs the loop over PRs 515, 514, …, one per state, each of whose close fails unless ``close_ok``."""
        stubs = pathlib.Path(tempfile.mkdtemp(prefix="promote-close-"))
        self.addCleanup(shutil.rmtree, stubs, True)
        (stubs / "gh").write_text(GH)
        (stubs / "gh").chmod(0o755)
        prs = {515 - i: state for i, state in enumerate(states)}
        self.calls = stubs / "calls"
        env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GH_CALLS": str(self.calls),
               "CLOSE_OK": "1" if close_ok else "", "new": "516", "ENVIRONMENT": "staging", **{f"PR_STATE_{n}": state for n, state in prs.items()}}
        # As the step runs it: under set -euo pipefail, fed the numbers superseded_promotions.py printed.
        numbers = "".join(f"{n}\\n" for n in prs)
        script = f"set -euo pipefail\nprintf '{numbers}' |\n{close_loop()}\n"
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)

    def test_a_pr_merged_before_its_close_does_not_fail_the_run(self):
        done = self.close("MERGED")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_pr_closed_before_its_close_does_not_fail_the_run(self):
        done = self.close("CLOSED")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_successful_close_comments_and_deletes_the_branch_without_asking_the_state(self):
        done = self.close("ERROR", close_ok=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls.read_text(),
                         "pr close 515 --delete-branch --comment Superseded by #516, the latest promotion to staging.\n")

    def test_a_pr_still_open_after_a_failed_close_fails_the_run(self):
        # Merging it later would roll the environment back to its older digest.
        done = self.close("OPEN")
        self.assertNotEqual(done.returncode, 0, done.stderr)

    def test_a_pr_whose_state_cannot_be_read_after_a_failed_close_fails_the_run(self):
        # A failed close and a failed `gh pr view` (a network error) leave the PR's state unknown: it may still be open.
        done = self.close("ERROR")
        self.assertNotEqual(done.returncode, 0, done.stderr)

    def test_a_state_printed_by_a_failed_query_fails_the_run(self):
        # `case "$(…)"` ignores the command's exit status: a MERGED printed before a failure would pass.
        done = self.close("MERGED_THEN_ERROR")
        self.assertNotEqual(done.returncode, 0, done.stderr)

    def test_an_open_pr_fails_the_run_even_when_a_later_pr_was_merged(self):
        done = self.close("OPEN", "MERGED")
        self.assertNotEqual(done.returncode, 0, done.stderr)

    def test_an_unreadable_state_fails_the_run_even_when_a_later_pr_was_merged(self):
        # The loop's status is its last iteration's: the unknown one must stop the loop, not be overwritten by the next.
        done = self.close("ERROR", "MERGED")
        self.assertNotEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
