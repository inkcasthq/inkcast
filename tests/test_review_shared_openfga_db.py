# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #304 (issue #296): what tests/test_shared_openfga_db.py leaves unproved, each case failing on the
code it was written against. The cases of scripts/deploy/openfga-db-credentials.sh went with the script (E3.F3.9.S7,
issue #323: the cluster generates the passwords; tests/test_env_secrets.py)."""

from __future__ import annotations

import subprocess
import unittest

from test_shared_openfga_db import PULL_SECRET, FakeOcCase


class TracedRunsNeverPrintTheSecret(FakeOcCase):
    """`bash -x script …` (debugging a failed run) or an exported SHELLOPTS=xtrace makes bash print every command
    after expansion (GNU Bash manual, "The Set Builtin" -x; "Bash Variables" SHELLOPTS): the token would be on stderr,
    in the terminal's scrollback or a CI log. The script turns the trace off first."""

    TRACES = {"bash -x": (["bash", "-x"], {}), "SHELLOPTS=xtrace": (["bash"], {"SHELLOPTS": "xtrace"})}

    def traced(self, how: str, script, *args: str) -> subprocess.CompletedProcess:
        shell, extra = self.TRACES[how]
        self.calls_file.write_text("")
        return subprocess.run([*shell, str(script), *args], env={**self.env, **extra}, capture_output=True, text=True,
                              timeout=60)

    def test_the_pull_secret_script_traced_never_prints_the_token(self):
        token = "ghp_" + "T9" * 18
        path = self.dir / "pat"
        path.write_text(token + "\n")
        for how in self.TRACES:
            with self.subTest(trace=how):
                done = self.traced(how, PULL_SECRET, "staging", "octocat", str(path))
                self.assertEqual(done.returncode, 0, done.stderr[-500:])
                self.assert_never_seen(token, done)


if __name__ == "__main__":
    unittest.main()
