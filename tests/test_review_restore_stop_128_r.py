# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #430 (issue #128): what the stop path of scripts/ops/restore.sh still gets wrong.

Each class is one finding, and fails on the PR's head (732cf8f):

* ``AStopInTheRestoresFirstMoments``: the script's SIGTERM, sent through `exec`, can land in the restore's first
  half-second, while it imports and before ``restore_main`` installs its handler (0.35-0.5 s), and PID 1 drops a
  signal it has no handler for: the stop is lost, and the restore the operator asked to stop goes on and swaps.
  Measured on kind with real signals: three Ctrl-Cs at the restore container's start, three restores that completed
  and replaced the database (the same Ctrl-C 0.6 s later stopped it); sending the signal again a second later still
  came too late for a 300 MB restore (three of three replaced). Waiting for PID 1 to handle SIGTERM (``SigCgt``)
  first: three of three stopped.
* ``AJobThatFailedEndsTheWait``: ``kubectl wait --for=condition=complete`` does not end when the Job fails; it runs to
  its timeout. A restore refused in its first second (a bad backup) kept the server at 0 for the whole
  RESTORE_TIMEOUT (on kind: 61 s for RESTORE_TIMEOUT=60, the Job Failed 2 s in; up to a day with the new bound)."""

import pathlib
import subprocess
import sys
import time
import unittest

from test_review_restore_script import _STUB_KUBECTL, _Script


def _stub(old: str, new: str) -> str:
    assert old in _STUB_KUBECTL, f"the stub has changed: {old!r} is not in it"
    return _STUB_KUBECTL.replace(old, new)


class _Variant(_Script):
    STUB = _STUB_KUBECTL

    def setUp(self):
        super().setUp()
        (self.dir / "kubectl").write_text(self.STUB)


# A stand-in for the restore: it imports for 0.7 s before it handles SIGTERM (``restore_main`` installs its handler
# 0.35-0.5 s in), then says "stopped" on a SIGTERM, or "done" two seconds on. A SIGTERM before its handler kills it
# here; as PID 1 in the image it would be dropped, and the restore would say done.
_RESTORE = r"""
import signal, sys, time
time.sleep(0.7)
signal.signal(signal.SIGTERM, lambda *_: (print("stopped", flush=True), sys.exit(1)))
time.sleep(2)
print("done", flush=True)
"""


@unittest.skipUnless(pathlib.Path("/proc/self/status").exists(), "reads /proc, as the pod's python does")
class AStopInTheRestoresFirstMoments(_Variant):
    """The python the script runs through `exec` (recorded by the stub), run against a process that has just started
    in place of PID 1: the SIGTERM must reach it once it handles one, not before."""

    STUB = _stub('  *"exec job/"*) touch "$CALLS.signalled" ;;',
                 '  *"exec job/"*) printf "%s" "${@: -1}" > "$CALLS.code"; touch "$CALLS.signalled" ;;')

    def test_the_stop_reaches_a_restore_that_has_just_started(self):
        run = self.run_script(FAKE_JOB="stops", INTERRUPT_ON="wait --for=condition=complete")
        code = pathlib.Path(f"{self.calls}.code").read_text()
        restore = subprocess.Popen([sys.executable, "-c", _RESTORE], stdout=subprocess.PIPE, text=True)
        pid = str(restore.pid)
        code = code.replace("/proc/1/", f"/proc/{pid}/").replace("os.kill(1,", f"os.kill({pid},")
        subprocess.run([sys.executable, "-c", code], check=True, timeout=10)
        out, _ = restore.communicate(timeout=10)
        self.assertEqual(out.strip(), "stopped", f"the SIGTERM reached the restore before its handler ({out!r}, "
                                                 f"exit {restore.returncode}): {run.calls}")


class AJobThatFailedEndsTheWait(_Variant):
    """The stub's wait behaves as kubectl's: it waits for Complete until its --timeout, whatever else the Job does;
    the Job (FAKE_JOB=refused) is Failed from the start, its log says restore.refused."""

    STUB = _stub('      *) echo "error: timed out waiting for the condition" >&2; exit 1 ;;',
                 '      refused) a="$*"; t="${a##*--timeout=}"; sleep "${t%s}"; echo "error: timed out" >&2; exit 1 ;;\n'
                 '      *) echo "error: timed out waiting for the condition" >&2; exit 1 ;;')
    STUB = STUB.replace("""  *'get job'*'type=="Failed"'*) [ "$FAKE_JOB" = stops ] && signalled && echo True ;;""",
                        """  *'get job'*'type=="Failed"'*) [ "$FAKE_JOB" = refused ] && echo True;"""
                        """ [ "$FAKE_JOB" = stops ] && signalled && echo True ;;""")
    STUB = STUB.replace("""      mute) ;;""", """      mute) ;;\n      refused) echo '{"event": "restore.refused"}' ;;""")

    def test_a_failed_job_is_seen_within_seconds_not_at_the_timeout(self):
        started = time.monotonic()
        run = self.run_script(FAKE_JOB="refused", RESTORE_TIMEOUT="30")
        took = time.monotonic() - started
        self.assertLess(took, 15, f"the script waited {took:.0f} s on a Job that had failed: {run.out}")
        self.assertIn("The restore job failed", run.out)
        self.assertIn("nothing was replaced", run.out)
        self.assertNotEqual(run.code, 0)



if __name__ == "__main__":
    unittest.main()
