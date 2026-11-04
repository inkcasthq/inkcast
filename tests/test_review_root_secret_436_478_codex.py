# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial regression tests for PR #478, with no cluster access."""

from __future__ import annotations

import shutil
import unittest

import test_review_root_secret_436_478 as prior


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


if __name__ == "__main__":
    unittest.main()
