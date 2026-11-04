# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""scripts/dev/env-secrets-e2e.sh, proof 4: the failed attempt's log is read with `kubectl logs --previous`, and the
kubelet can answer "unable to retrieve container logs for containerd://..." while its cached status still names a
dead container the runtime has already removed (issue #435; the kubelet resolves --previous from its own status,
pkg/kubelet/kubelet_pods.go:1640,1565-1568 at v1.37.0, and asks the runtime for that container,
kuberuntime_container.go:1308-1311). The script's reader, `init_log_of`, retries that one error with a deadline,
prints how many attempts it took, fails at once on any other error, and writes the log file only from a read that
succeeded. Driven here with a stub kubectl, the function cut out of the script, so no cluster is needed."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dev" / "env-secrets-e2e.sh"
UNABLE = "unable to retrieve container logs for containerd://526ae0c7d3f1"
LOG = "env-secrets: keycloak/inkcast-staging-realm identity-client-secret and inkcast-staging/inkcast-secrets oidc-client-secret disagree\n"

# Fails FAILURES times with the kubelet's error, or for good with FAIL_WITH, then prints the log. Every call's
# arguments go to $CALLS, one line each.
STUB_KUBECTL = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "$CALLS"
    n=$(wc -l < "$CALLS")
    if [ -n "${FAIL_WITH:-}" ]; then echo "$FAIL_WITH" >&2; exit 1; fi
    if [ "$n" -le "${FAILURES:-0}" ]; then echo "error: %s" >&2; printf 'Error from server: %s\\n' "$UNABLE" >&2; exit 1; fi
    printf '%s' "$LOG"
    """)


def init_log_of() -> str:
    """The function's text, cut out of the script by its name."""
    found = re.search(r"^init_log_of\(\) \{.*?^\}$", SCRIPT.read_text(), re.S | re.M)
    assert found, "init_log_of() is not defined in scripts/dev/env-secrets-e2e.sh"
    return found[0]


class ReadingTheFailedAttemptsLog(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="inkcast-435-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)], check=False))
        (self.dir / "kubectl").write_text(STUB_KUBECTL)
        (self.dir / "kubectl").chmod(0o755)
        self.calls = self.dir / "calls"
        self.calls.touch()
        self.work = self.dir / "work"
        self.work.mkdir()

    def run_reader(self, *args: str, failures: int = 0, fail_with: str = "", seconds: str = "30") -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.dir}:{os.environ.get('PATH', '')}", "CALLS": str(self.calls),
               "FAILURES": str(failures), "FAIL_WITH": fail_with, "UNABLE": UNABLE, "LOG": LOG,
               "LOG_RETRY_SECONDS": seconds, "LOG_RETRY_SLEEP": "0.1"}
        program = f'set -euo pipefail\nk=kubectl\nns=inkcast-staging\nwork="{self.work}"\n{init_log_of()}\ninit_log_of "$@"\n'
        return subprocess.run(["bash", "-c", program, "reader", *args], env=env, capture_output=True, text=True, timeout=60)

    def test_the_read_is_retried_while_containerd_cannot_retrieve_the_log_and_the_count_is_printed(self):
        done = self.run_reader("openfga-7d9f-x2k4q", "--previous", failures=2)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.work / "init.log").read_text(), LOG)
        self.assertIn("read the log of openfga-7d9f-x2k4q --previous (attempt 3)", done.stdout)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls, ["-n inkcast-staging logs -c env-secrets openfga-7d9f-x2k4q --previous"] * 3)

    def test_a_first_read_that_succeeds_is_one_attempt(self):
        done = self.run_reader("openfga-7d9f-x2k4q")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("(attempt 1)", done.stdout)
        self.assertEqual(self.calls.read_text().splitlines(), ["-n inkcast-staging logs -c env-secrets openfga-7d9f-x2k4q"])

    def test_the_deadline_fails_the_proof_naming_the_pod_and_the_error_and_writes_no_file(self):
        done = self.run_reader("openfga-7d9f-x2k4q", "--previous", failures=1000, seconds="1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("openfga-7d9f-x2k4q --previous", done.stderr)
        self.assertIn(UNABLE, done.stderr)
        self.assertRegex(done.stderr, r"after \d+ attempts over 1 s")
        self.assertFalse((self.work / "init.log").exists(), "an empty or partial file written as the log")
        self.assertGreater(len(self.calls.read_text().splitlines()), 1)

    def test_any_other_error_fails_at_once(self):
        done = self.run_reader("openfga-7d9f-x2k4q", "--previous", fail_with='Error from server (NotFound): pods "openfga-7d9f-x2k4q" not found')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('pods "openfga-7d9f-x2k4q" not found', done.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1, "retried an error that is not the kubelet's")
        self.assertFalse((self.work / "init.log").exists())
