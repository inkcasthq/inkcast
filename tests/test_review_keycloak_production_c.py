# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Confirmation review of the production Keycloak component (E9.F9.1.S3, issue #386, PR #391): each test failed on the
PR's head, 2802230."""

import unittest

import test_keycloak_production_component as component

SCRIPT = component.ROOT / "scripts" / "dev" / "keycloak-production-e2e.sh"

# Proof lines of the script that, commented out or removed, leave a script that still runs and passes, so only the
# wiring test can notice them gone: each is located by its text, not by the test's own patterns.
UNGUARDED_PROOFS = {
    "step 5 asks the recreated pod": 'issuer="$(discovery inkcast-staging 28444)"',
    "step 5 compares its issuer": '[ "$issuer" = "$hostname/realms/inkcast-staging" ] ||',
    "step 6 checks each probe's answer": 'has "$out" "exit=$4" ||',
    "step 1 refuses start-dev and --optimized": 'if has "$args" "start-dev" || has "$args" "optimized"; then',
    "step 3 refuses no connections at all": 'case "$ssl" in 0/*|*/0)',
}


class EveryProofLineIsACommand(unittest.TestCase):
    """K11 (cce6956) says the CI-wiring test fails when a proof line is commented out or removed. It does for the lines
    PROOF_COMMANDS lists; with any of these commented out, every test passed on 2802230, and the script still passes on
    kind while proving less (step 5 would delete keycloak-0 and never ask it anything)."""

    def test_each_proof_line_commented_or_removed_fails_the_wiring_check(self):
        script = SCRIPT.read_text()
        for what, text in UNGUARDED_PROOFS.items():
            (line,) = [line for line in script.splitlines() if text in line]
            for how, mutant in (("commented", script.replace(line, "# " + line)), ("removed", script.replace(line + "\n", ""))):
                with self.subTest(proof=what, mutant=how):
                    self.assertNotEqual(mutant, script)
                    with self.assertRaises(AssertionError, msg=f"{what}: the line {how} passed the wiring check"):
                        component.assert_proof_commands(self, mutant)


if __name__ == "__main__":
    unittest.main()
