# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial regression tests for PR #478, with no cluster access."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import test_review_root_secret_436_478 as prior


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "lab" / "root-secret-copy.sh"
DRILL = ROOT / "scripts" / "ops" / "root-secret-drill.sh"


@unittest.skipUnless(shutil.which("kubectl"), "the drill stub answers JSONPath with kubectl")
class DrillLateLoss(unittest.TestCase):
    """Reuse the existing stateful oc stub and its real kubectl JSONPath queries."""

    setUp = prior.DrillAgainstAStub.setUp
    save = prior.DrillAgainstAStub.save
    secret = prior.DrillAgainstAStub.secret
    drill = prior.DrillAgainstAStub.drill

    def test_secret_removed_during_later_checks_is_recovered_or_copy_kept(self):
        stub = self.dir / "oc"
        code = stub.read_text()
        before = 'elif verb == "logs":\n    print('
        after = ('elif verb == "logs":\n'
                 '    if scenario == "late-loss":\n'
                 '        path("inkcast-staging", "secret", "inkcast-secrets").unlink()\n'
                 '    print(')
        self.assertIn(before, code)
        stub.write_text(code.replace(before, after, 1))

        done = self.drill("late-loss")
        copies = list(self.tmp.iterdir())
        self.assertNotEqual(done.returncode, 0, "a late loss must fail the drill")
        self.assertNotIn("Drill passed", done.stdout)
        self.assertTrue(self.secret() is not None or copies,
                        "late loss removed the Secret and EXIT removed the only safety copy:\n" + done.stderr)


class PythonPreflight(unittest.TestCase):
    """Both scripts run their interpreter before any cluster call, so neither a non-Python executable nor an old
    Python (macOS ships 3.9, the reason the scripts take the venv) gets as far as oc."""

    def run_with(self, script, fake_body, *args):
        with tempfile.TemporaryDirectory(prefix="r478-python-", dir=ROOT) as temporary:
            directory = pathlib.Path(temporary)
            fake_python = directory / "fake-python"
            fake_python.write_text(f"#!/bin/sh\n{fake_body}\n")
            fake_python.chmod(0o755)
            oc = directory / "oc"
            oc.write_text(f"#!/bin/sh\nprintf '%s\\n' called >> '{directory / 'oc.log'}'\nexit 99\n")
            oc.chmod(0o755)
            helm = directory / "helm"
            helm.write_text("#!/bin/sh\nexit 0\n")
            helm.chmod(0o755)
            done = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, timeout=20,
                                  env={**os.environ, "INKCAST_PYTHON": str(fake_python), "KUBECTL": str(oc),
                                       "PATH": f"{directory}:{os.environ['PATH']}", "TMPDIR": str(directory)})
            return done, (directory / "oc.log").exists()

    def test_an_executable_that_is_not_python_or_is_too_old_stops_before_cluster_access(self):
        # "exit 0": not Python at all. "echo False": what Python 3.9 answers to the version check.
        for script, args in ((INSTALL, ()), (DRILL, ("inkcast-staging",))):
            for fake_body in ("exit 0", "echo False"):
                with self.subTest(script=script.name, fake=fake_body):
                    done, called = self.run_with(script, fake_body, *args)
                    self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                    self.assertIn("is not Python 3.12 or later", done.stderr)
                    self.assertFalse(called, "the script called oc before validating Python")


if __name__ == "__main__":
    unittest.main()
