# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""scripts/ops/restore.sh never leaves the server running beside a restore Job (#111).

A stub ``kubectl`` on PATH records every call (one line per call in ``$CALLS``), keeps the manifest piped to
``apply`` and plays one of four Jobs, chosen by ``$FAKE_JOB``: ``succeeds``, ``fails``, ``never`` (never finishes;
its pod is there until the Job is deleted) and ``stuck`` (never finishes, and its pod survives the deletion)."""

import os
import pathlib
import re
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ops/restore.sh"

_STUB_KUBECTL = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
case "$*" in
  *imagePullPolicy*|*securityContext*) echo "" ;;
  *"get cronjob"*) echo "ghcr.io/inkcasthq/inkcast@sha256:0123" ;;
  *"get deploy inkcast"*) echo "1" ;;
  *"get pod -l app.kubernetes.io/component=server"*) ;;
  *"apply -f -"*) cat > "$CALLS.manifest" ;;
  *"wait --for=condition=complete"*)
    case "$FAKE_JOB" in
      succeeds) exit 0 ;;
      hangs) sleep 30; exit 1 ;;
      *) echo "timed out waiting for the condition" >&2; exit 1 ;;
    esac ;;
  *"logs job/"*) echo '{"event": "restore.log"}' ;;
  *'get job'*'type=="Failed"'*) [ "$FAKE_JOB" = fails ] && echo True ;;
  *'get job'*'type=="Complete"'*) [ "$FAKE_JOB" = succeeds ] && echo True ;;
  *"delete job"*) touch "$CALLS.deleted" ;;
  *"get pod -l job-name="*)
    case "$FAKE_JOB" in
      stuck) echo "pod/inkcast-restore-x-abcde" ;;
      never|hangs) [ -e "$CALLS.deleted" ] || echo "pod/inkcast-restore-x-abcde" ;;
    esac ;;
esac
exit 0
"""


class _Run:
    def __init__(self, code: int, out: str, calls: list[str], manifest: str):
        self.code, self.out, self.calls, self.manifest = code, out, calls, manifest

    def index(self, prefix: str) -> int:
        """The position of the first call starting with ``prefix``; fails the test when there is none."""
        for i, call in enumerate(self.calls):
            if call.startswith(prefix):
                return i
        raise AssertionError(f"no kubectl call starts with {prefix!r}: {self.calls}")

    def has(self, prefix: str) -> bool:
        return any(c.startswith(prefix) for c in self.calls)


SCALE_DOWN = "-n inkcast scale deploy/inkcast --replicas=0"
SCALE_UP = "-n inkcast scale deploy/inkcast --replicas=1"
DELETE = "-n inkcast delete job inkcast-restore-"
WAIT = "-n inkcast wait --for=condition=complete job/inkcast-restore-"


class RestoreScript(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="restore-script-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)], check=False))
        stub = self.dir / "kubectl"
        stub.write_text(_STUB_KUBECTL)
        stub.chmod(0o755)
        self.calls = self.dir / "calls"
        self.calls.write_text("")
        self.env = {**os.environ, "PATH": f"{self.dir}:{os.environ.get('PATH', '')}", "CALLS": str(self.calls),
                    "RESTORE_TIMEOUT": "2"}
        self.env.pop("KUBECTL", None)

    def start(self, job: str, *args: str) -> subprocess.Popen:
        return subprocess.Popen(["bash", str(SCRIPT), "inkcast", *args], env={**self.env, "FAKE_JOB": job},
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)

    def result(self, proc: subprocess.Popen) -> _Run:
        out, _ = proc.communicate(timeout=60)
        manifest = self.calls.with_suffix(".manifest")
        return _Run(proc.returncode, out, self.calls.read_text().splitlines(),
                    manifest.read_text() if manifest.exists() else "")

    def run_script(self, job: str, *args: str) -> _Run:
        return self.result(self.start(job, *args))

    # 1. The Job never finishes.
    def test_a_job_that_never_finishes_is_deleted_before_the_server_comes_back(self):
        run = self.run_script("never")
        self.assertNotEqual(run.code, 0, run.out)
        self.assertIn("nothing was replaced", run.out.lower())
        self.assertNotIn("Restored.", run.out)
        self.assertLess(run.index(DELETE), run.index(SCALE_UP), run.calls)
        delete = run.calls[run.index(DELETE)]
        self.assertIn("--wait=true", delete)
        self.assertIn("--cascade=foreground", delete)
        self.assertLess(run.index(DELETE), run.index("-n inkcast get pod -l job-name="), "its pod was not confirmed gone")
        self.assertLess(run.index("-n inkcast get pod -l job-name="), run.index(SCALE_UP))

    def test_the_wait_and_the_jobs_deadline_come_from_restore_timeout(self):
        run = self.run_script("never")
        self.assertIn("--timeout=2s", run.calls[run.index(WAIT)])
        self.assertRegex(run.manifest, re.compile(r"^\s*activeDeadlineSeconds: 2$", re.M))

    def test_restore_timeout_must_be_a_positive_integer(self):
        for bad in ("0", "5m", "-1"):
            with self.subTest(bad=bad):
                done = subprocess.run(["bash", str(SCRIPT), "inkcast"], env={**self.env, "RESTORE_TIMEOUT": bad},
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(self.calls.read_text(), "", "kubectl was called with a bad timeout")

    # 2. The Job succeeds.
    def test_a_completed_job_scales_the_server_up_and_reports_a_restore(self):
        run = self.run_script("succeeds")
        self.assertEqual(run.code, 0, run.out)
        self.assertIn("Restored.", run.out)
        self.assertLess(run.index(SCALE_DOWN), run.index("-n inkcast apply"))
        self.assertLess(run.index(WAIT), run.index(SCALE_UP))
        self.assertFalse(run.has(DELETE), "a completed Job was deleted")
        self.assertRegex(run.manifest, re.compile(r"^\s*activeDeadlineSeconds: 2$", re.M))

    # 3. The Job fails.
    def test_a_failed_job_is_gone_before_the_server_comes_back_and_the_script_exits_non_zero(self):
        run = self.run_script("fails")
        self.assertNotEqual(run.code, 0, run.out)
        self.assertIn("The restore job failed", run.out)
        self.assertIn("nothing was replaced", run.out.lower())
        self.assertNotIn("Restored.", run.out)
        self.assertLess(run.index(DELETE), run.index(SCALE_UP), run.calls)
        self.assertLess(run.index("-n inkcast get pod -l job-name="), run.index(SCALE_UP))

    # A pod of the Job that will not go: the server stays down rather than share the volume with it.
    def test_the_server_stays_down_while_a_pod_of_the_job_survives(self):
        run = self.run_script("stuck")
        self.assertNotEqual(run.code, 0, run.out)
        self.assertTrue(run.has(DELETE))
        self.assertFalse(run.has(SCALE_UP), f"the server came back beside a live restore pod: {run.calls}")
        self.assertIn("stays at 0 replicas", run.out)

    # An interrupt while waiting: the Job goes before the server comes back.
    def test_an_interrupt_during_the_wait_deletes_the_job_before_scaling_up(self):
        proc = self.start("hangs")
        deadline = time.monotonic() + 20
        while WAIT not in self.calls.read_text() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(WAIT, self.calls.read_text(), "the script never reached its wait")
        time.sleep(0.2)
        os.killpg(proc.pid, signal.SIGINT)
        run = self.result(proc)
        self.assertNotEqual(run.code, 0, run.out)
        self.assertIn("nothing was replaced", run.out.lower())
        self.assertLess(run.index(DELETE), run.index(SCALE_UP), run.calls)
        self.assertLess(run.index("-n inkcast get pod -l job-name="), run.index(SCALE_UP))

    # --list leaves the server alone whatever the Job does.
    def test_listing_never_touches_the_server(self):
        for job in ("succeeds", "never"):
            with self.subTest(job=job):
                self.calls.write_text("")
                run = self.run_script(job, "--list")
                self.assertEqual(run.code, 0 if job == "succeeds" else 1, run.out)
                self.assertFalse(any("scale deploy" in c for c in run.calls), run.calls)


if __name__ == "__main__":
    unittest.main()
