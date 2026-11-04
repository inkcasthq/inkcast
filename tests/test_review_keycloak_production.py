# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of the production Keycloak component (E9.F9.1.S3, issue #386, PR #391): each test failed on the
PR's head."""

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "deploy" / "kustomize" / "components" / "keycloak-production"
SCRIPT = ROOT / "scripts" / "dev" / "keycloak-production-e2e.sh"


def policy() -> dict:
    (found,) = [d for d in yaml.safe_load_all((COMPONENT / "networkpolicy.yaml").read_text()) if d]
    return found


def step(number: str) -> str:
    """The lines of the e2e script's step `number`, up to the next step or the PASS line."""
    text = SCRIPT.read_text()
    start = text.index(f'echo "-- {number}.')
    ends = [i for i in (text.find('echo "-- ', start + 1), text.find('echo "PASS', start)) if i > 0]
    return text[start:min(ends)]


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

    def test_the_recreated_pod_is_asked_through_a_forward_of_its_own(self):
        """Step 5 asked through step 4's `port-forward svc/keycloak-service`, which is bound to one pod. On kind (-v=6:
        `pods/keycloak-1/portforward`) it passed while keycloak-1, never restarted, answered; with keycloak-0 Ready
        longest it was bound to keycloak-0, died with it ("error: lost connection to pod"), and the step failed. A wait
        on the deleted pod's name before the StatefulSet makes it again fails at once (NotFound)."""
        five = step("5")
        self.assertIn("port-forward pod/keycloak-0", five)
        self.assertIn("--for=create", five)
        port = re.search(r"port-forward pod/keycloak-0 (\d+):8443", five).group(1)
        self.assertIn(f"discovery inkcast-staging {port}", five, "the discovery goes through the new pod's forward")


if __name__ == "__main__":
    unittest.main()
