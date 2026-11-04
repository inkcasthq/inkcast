# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Confirmation review of the production Keycloak component (E9.F9.1.S3, issue #386, PR #391): each test failed on the
PR's head, 2802230."""

import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

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


# Kustomizations that render the component without naming it in their own `components` list, or under the other file
# name kustomize reads; each renders the component's unresolvable hostname, unpatched.
NAMESPACE = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: keycloak\n  labels: {pod-security.kubernetes.io/enforce: restricted}\n"
INDIRECT_USERS = {
    "components/kc-wrap/kustomization.yaml": "apiVersion: kustomize.config.k8s.io/v1alpha1\nkind: Component\n"
                                             "components: [../keycloak-production]\n",
    "overlays/via-wrapper/kustomization.yaml": "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
                                               "namespace: keycloak\nresources: [namespace.yaml]\n"
                                               "components: [../../components/kc-wrap]\n",
    "overlays/via-wrapper/namespace.yaml": NAMESPACE,
    "overlays/yml-name/kustomization.yml": "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
                                           "namespace: keycloak\nresources: [namespace.yaml]\n"
                                           "components: [../../components/keycloak-production]\n",
    "overlays/yml-name/namespace.yaml": NAMESPACE,
    "overlays/on-the-ci-overlay/kustomization.yaml": "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
                                                     "resources: [../ci-keycloak-production]\npatches:\n  - patch: |-\n"
                                                     "      apiVersion: k8s.keycloak.org/v2beta1\n      kind: Keycloak\n"
                                                     "      metadata: {name: keycloak}\n"
                                                     "      spec: {hostname: {hostname: https://keycloak.example.invalid}}\n",
}


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations")
class EveryKustomizationThatRendersTheComponent(unittest.TestCase):
    """K6 (7fe6ca7) says the hostname test covers every kustomization that uses the component. Its scan finds those
    that name the component in their own `components` list, in a file named kustomization.yaml. On 2802230 it found
    none of these three, each rendering `https://keycloak.example.invalid` (kubectl kustomize), so the Rendered test
    passed with them in the tree: a Component that includes this one, a kustomization.yml, an overlay on the CI
    overlay."""

    def test_the_scan_finds_a_kustomization_however_it_reaches_the_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            deploy = pathlib.Path(tmp) / "deploy"
            shutil.copytree(component.ROOT / "deploy", deploy)
            kustomize = deploy / "kustomize"
            for name, text in INDIRECT_USERS.items():
                (kustomize / name).parent.mkdir(parents=True, exist_ok=True)
                (kustomize / name).write_text(text)
            with mock.patch.object(component, "KUSTOMIZE", kustomize):
                found = component.kustomizations_using(kustomize / "components" / "keycloak-production")
        for name in ("overlays/via-wrapper", "overlays/yml-name", "overlays/on-the-ci-overlay"):
            self.assertIn(name, found, f"{name} renders the component, unpatched")
        self.assertNotIn("components/kc-wrap", found, "a Component is rendered by what uses it")
        self.assertIn("overlays/ci-keycloak-production", found)


if __name__ == "__main__":
    unittest.main()
