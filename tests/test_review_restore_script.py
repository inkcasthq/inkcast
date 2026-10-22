# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #111 (PR #120): scripts/ops/restore.sh must never leave the server running beside a restore
Job, and says "Restored." only when the Job completed.

A stub ``kubectl`` on PATH records every call (one line per call in ``$CALLS``). ``$FAKE_JOB`` picks the Job:
``succeeds``, ``late`` (the wait gives up, and the Job's Complete condition is set when read next), ``stuck`` (never
finishes; its pod survives the deletion) and ``pending`` (never finishes and has no pod yet). ``$INTERRUPT_ON`` names
a call during which Ctrl-C is pressed (SIGINT to the script's process group, once); ``$DELETE=refused`` makes the
Job's deletion fail; ``$SERVER_PODS=unreadable`` makes listing the server's pods fail."""

import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ops/restore.sh"

_STUB_KUBECTL = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
if [ -n "${INTERRUPT_ON:-}" ] && [[ "$*" == *"$INTERRUPT_ON"* ]] && [ ! -e "$CALLS.interrupted" ]; then
  touch "$CALLS.interrupted"
  kill -INT 0                     # Ctrl-C: the whole process group, this kubectl included
  sleep 5
fi
case "$*" in
  *imagePullPolicy*|*securityContext*) echo "" ;;
  *"get cronjob"*) echo "ghcr.io/inkcasthq/inkcast@sha256:0123" ;;
  *"get deploy inkcast"*) echo "1" ;;
  *"pod -l app.kubernetes.io/component=server"*)
    if [ "${SERVER_PODS:-}" = unreadable ]; then echo "Error from server (Forbidden): pods is forbidden" >&2; exit 1; fi ;;
  *"apply -f -"*) cat > /dev/null ;;
  *"wait --for=condition=complete"*)
    [ "$FAKE_JOB" = succeeds ] || { echo "error: timed out waiting for the condition" >&2; exit 1; } ;;
  *"logs job/"*) echo '{"event": "restore.log"}' ;;
  *'get job'*'type=="Failed"'*) ;;
  *'get job'*'type=="Complete"'*) case "$FAKE_JOB" in succeeds|late) echo True ;; esac ;;
  *"delete job"*)
    if [ "${DELETE:-}" = refused ]; then echo "Error from server (Forbidden): jobs.batch is forbidden" >&2; exit 1; fi
    touch "$CALLS.deleted" ;;
  *"get job"*) [ -e "$CALLS.deleted" ] || echo "job.batch/inkcast-restore-x" ;;
  *"get pod -l job-name="*) [ "$FAKE_JOB" = stuck ] && echo "pod/inkcast-restore-x-abcde" ;;
esac
exit 0
"""

SCALE_DOWN = "-n inkcast scale deploy/inkcast --replicas=0"
SCALE_UP = "-n inkcast scale deploy/inkcast --replicas=1"
APPLY = "-n inkcast apply"
DELETE = "-n inkcast delete job inkcast-restore-"


class _Run:
    def __init__(self, code: int, out: str, calls: list[str]):
        self.code, self.out, self.calls = code, out, calls

    def index(self, prefix: str) -> int:
        for i, call in enumerate(self.calls):
            if call.startswith(prefix):
                return i
        raise AssertionError(f"no kubectl call starts with {prefix!r}: {self.calls}")

    def has(self, prefix: str) -> bool:
        return any(c.startswith(prefix) for c in self.calls)


class _Script(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="review-restore-script-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)], check=False))
        stub = self.dir / "kubectl"
        stub.write_text(_STUB_KUBECTL)
        stub.chmod(0o755)
        self.calls = self.dir / "calls"
        self.calls.write_text("")
        self.env = {**os.environ, "PATH": f"{self.dir}:{os.environ.get('PATH', '')}", "CALLS": str(self.calls),
                    "RESTORE_TIMEOUT": "2"}
        for name in ("KUBECTL", "FAKE_JOB", "INTERRUPT_ON", "DELETE", "SERVER_PODS"):
            self.env.pop(name, None)

    def run_script(self, **env: str) -> _Run:
        proc = subprocess.Popen(["bash", str(SCRIPT), "inkcast"], env={**self.env, **env}, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
        out, _ = proc.communicate(timeout=60)
        return _Run(proc.returncode, out, self.calls.read_text().splitlines())


class TheServerMustBeSeenGone(_Script):
    """After scaling the server to 0 the script lists its pods, and any left means no restore. A list that fails (an
    API error, a role without `list pods`, under which `wait --for=delete` fails at once too) read as "none left":
    the Job then ran with `--force` beside a server pod that may still be shutting down and writing."""

    def test_a_failed_list_of_the_servers_pods_stops_the_restore(self):
        run = self.run_script(FAKE_JOB="succeeds", SERVER_PODS="unreadable")
        self.assertFalse(run.has(APPLY), f"the restore Job ran without seeing the server's pods gone: {run.calls}")
        self.assertNotEqual(run.code, 0, run.out)
        self.assertNotIn("Restored.", run.out)
        self.assertLess(run.index(SCALE_DOWN), run.index(SCALE_UP), "the server was not brought back")


class AnInterruptedScaleDownIsUndone(_Script):
    """``scaled_down`` was set after `kubectl scale --replicas=0` returned. Ctrl-C while that call runs (the API has
    taken it, kubectl has not returned) ran the exit trap with the flag still false: the server stayed at 0 replicas
    and the script said only that it stopped, not that the server was down or how to bring it back."""

    def test_ctrl_c_during_the_scale_down_scales_the_server_back(self):
        run = self.run_script(FAKE_JOB="succeeds", INTERRUPT_ON="scale deploy/inkcast --replicas=0")
        self.assertNotEqual(run.code, 0, run.out)
        self.assertFalse(run.has(APPLY), run.calls)
        self.assertLess(run.index(SCALE_DOWN), run.index(SCALE_UP), f"the server was left at 0: {run.calls}")


class AJobThatCompletedAsTheWaitGaveUp(_Script):
    """`kubectl wait` gave up (its timeout, or a watch error) and the Job's Complete condition was set by then, in the
    instant before, or with the watch event still in flight. The exit trap reads the condition for that reason, but
    the main path never did: it deleted the completed Job and said "Nothing was replaced: the server runs on the
    database it had" while the server came back on the restored one."""

    def test_a_job_complete_when_the_wait_gives_up_is_reported_as_a_restore(self):
        run = self.run_script(FAKE_JOB="late")
        self.assertNotIn("nothing was replaced", run.out.lower())
        self.assertFalse(run.has(DELETE), f"a completed Job was deleted: {run.calls}")
        self.assertIn("Restored.", run.out)
        self.assertEqual(run.code, 0, run.out)


class AJobThatCouldNotBeDeleted(_Script):
    """stop_job ignores the delete's exit status (`|| true`) and decides on the pod list alone. When the delete fails
    (refused: a role that may create Jobs but not delete them, which the script never needed before; or an API
    error) and the Job has no pod yet (Ctrl-C in its first moments, or a webhook or quota holding its pod back), the
    list is empty: the server came back while the Job was still there, and its pod then swaps the database under
    the running server."""

    def test_the_server_stays_down_while_the_job_itself_is_still_there(self):
        run = self.run_script(FAKE_JOB="pending", DELETE="refused", INTERRUPT_ON="wait --for=condition=complete")
        self.assertNotEqual(run.code, 0, run.out)
        self.assertTrue(run.has(DELETE), run.calls)
        self.assertFalse(run.has(SCALE_UP), f"the server came back beside a Job that was not deleted: {run.calls}")
        self.assertIn("stays at 0 replicas", run.out)
        self.assertNotIn("nothing was replaced", run.out.lower())


if __name__ == "__main__":
    unittest.main()
