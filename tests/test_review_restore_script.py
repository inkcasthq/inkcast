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
import shutil
import signal
import sqlite3
import subprocess
import sys
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


class AnInterruptWhileTheJobIsBeingStopped(_Script):
    """stop_job cleared ``applied`` before it had looked for the Job's pod. On the main path (the Job did not complete
    within the wait), Ctrl-C during that look (after minutes of waiting on the delete) ran the exit trap with
    ``applied`` false: it skipped the Job and scaled the server back up beside the pod that would not go."""

    def test_ctrl_c_during_the_pod_check_keeps_the_server_down_beside_a_surviving_pod(self):
        run = self.run_script(FAKE_JOB="stuck", INTERRUPT_ON="get pod -l job-name=")
        self.assertNotEqual(run.code, 0, run.out)
        self.assertTrue(run.has(DELETE), run.calls)
        self.assertFalse(run.has(SCALE_UP), f"the server came back beside a live restore pod: {run.calls}")
        self.assertIn("stays at 0 replicas", run.out)


# Runs restore_backup and SIGKILLs itself just before its <step>-th rename, link or removal in the data directory (an
# audit hook runs before the operation), as the kubelet's SIGKILL can land at any instant.
_KILLED_BEFORE_STEP = r"""
import os, pathlib, signal, sys
backup, database, step = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
from inkcast.libs.db.backup import restore_backup
data, seen = str(database.parent), 0

def kill_before(event, args):
    global seen
    if event in ("os.rename", "os.link", "os.remove") and str(args[0]).startswith(data):
        seen += 1
        if seen == step:
            os.kill(os.getpid(), signal.SIGKILL)

sys.addaudithook(kill_before)
restore_backup(backup, database, force=True)
"""


class ARestoreKilledDuringTheSwap(unittest.TestCase):
    """The Job's deadline and stop_job now stop a restore that runs late, and the pod's end is a SIGKILL at any
    instant (`inkcast` is PID 1 in the image and sets no SIGTERM handler for `restore`, so the SIGTERM is ignored and
    the grace period ends in SIGKILL). restore_backup moved the current database aside, then moved the restored copy
    in: killed between the two, no database is at its path, the server creates an empty one as it starts, and the
    script says "Nothing was replaced: the server runs on the database it had"."""

    @staticmethod
    def users(path: pathlib.Path) -> int:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()

    def test_a_kill_before_any_step_of_the_swap_leaves_the_old_or_the_new_database(self):
        from inkcast.libs.db import Store
        from inkcast.libs.db.backup import take_backup
        base = pathlib.Path(tempfile.mkdtemp(prefix="review-restore-swap-"))
        self.addCleanup(shutil.rmtree, base, True)
        db = base / "data" / "inkcast.db"
        db.parent.mkdir()
        store = Store(str(db), "k" * 40)                              # WAL mode, as the server runs it
        store.upsert_user("local:owner", "owner", None, None)
        backup = take_backup(db, base / "backups").path              # the new database: one user
        store.upsert_user("local:second", "second", None, None)      # the old one: two
        store.close()
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        killed = 0
        for step in range(1, 10):
            run = base / f"run-{step}"
            shutil.copytree(base / "data", run / "data")
            shutil.copytree(base / "backups", run / "backups")
            target = run / "data" / "inkcast.db"
            done = subprocess.run([sys.executable, "-c", _KILLED_BEFORE_STEP, str(run / "backups" / backup.name),
                                   str(target), str(step)], env=env, capture_output=True, text=True, timeout=60)
            left = sorted(p.name for p in target.parent.iterdir())
            self.assertTrue(target.exists(), f"killed before step {step}: no database at its path, only {left}")
            if done.returncode == 0:                                  # no step {step}: the restore went through
                self.assertEqual(self.users(target), 1)
                break
            self.assertEqual(done.returncode, -signal.SIGKILL, done.stderr)
            killed += 1
            self.assertIn(self.users(target), (1, 2), f"killed before step {step}")
        self.assertGreater(killed, 0, "the restore was never killed: the hook saw no step")


if __name__ == "__main__":
    unittest.main()
