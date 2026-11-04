# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of the production Keycloak component (E9.F9.1.S3, issue #386, PR #391): each test failed on the
PR's head."""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "deploy" / "kustomize" / "components" / "keycloak-production"


def policy() -> dict:
    (found,) = [d for d in yaml.safe_load_all((COMPONENT / "networkpolicy.yaml").read_text()) if d]
    return found


class ReviewKeycloakProduction(unittest.TestCase):
    def test_the_database_policy_names_namespaces_by_the_label_the_api_server_sets(self):
        """A namespace peer selected by app.kubernetes.io/name=cloudnative-pg admits every namespace that carries that
        label: on kind, a pod in a namespace labelled so, with the operator's pod label, opened 5432 on keycloak-db-1
        ("accepting connections"). kubernetes.io/metadata.name is set by the API server to the namespace's name, and
        is what CloudNativePG's own sample (docs/src/samples/networkpolicy-example.yaml at v1.30.1) and
        shared/inkcast-db/networkpolicy.yaml select by."""
        for rule in policy()["spec"]["ingress"]:
            for peer in rule["from"]:
                if "namespaceSelector" in peer:
                    self.assertEqual(set(peer["namespaceSelector"]["matchLabels"]), {"kubernetes.io/metadata.name"}, peer)
                    self.assertNotIn("matchExpressions", peer["namespaceSelector"], peer)


if __name__ == "__main__":
    unittest.main()
