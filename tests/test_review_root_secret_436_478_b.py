# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #478 (issue #436), second pass over a124d123..74614060: the guards the rework added, held by tests
that fail when a guard stops guarding. No cluster is reached: the scripts' own blocks run against stub commands."""

from __future__ import annotations

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "deploy" / "kustomize" / "shared" / "seaweedfs-admission" / "policy.yaml"
SETUP_ACCOUNT = "system:serviceaccount:inkcast-seaweedfs:seaweedfs-setup"


class AdmissionPolicyExpressions(unittest.TestCase):
    def test_each_expression_is_the_one_evaluated(self):
        """The policy's CEL was evaluated in review by the API server's own code (k8s.io/apiserver v0.37.1,
        validating.compilePolicy and Validate) against crafted Secrets: a token Secret, an Opaque one under another
        name or the swapped namespace, an annotated one, a generateName, a missing type are each denied, the two
        credentials admitted. CI has no CEL evaluator, and a test that looks for substrings passes an expression
        made always true (`... || true`), so each expression is held whole: changing one means evaluating it again."""
        policy = next(yaml.safe_load_all(POLICY.read_text()))
        self.assertEqual([c["expression"] for c in policy["spec"]["matchConditions"]],
                         [f"request.userInfo.username == '{SETUP_ACCOUNT}'"])
        self.assertEqual([v["expression"] for v in policy["spec"]["validations"]], [
            "object.type == 'Opaque'",
            "(request.namespace == 'openshift-adp' && object.metadata.name == 'cloud-credentials-root-secrets')"
            " || (request.namespace == 'inkcast-seaweedfs' && object.metadata.name == 'seaweedfs-s3-config')",
            "!has(object.metadata.annotations) || !('kubernetes.io/service-account.name' in object.metadata.annotations)",
        ])


if __name__ == "__main__":
    unittest.main()
