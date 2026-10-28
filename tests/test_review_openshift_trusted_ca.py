# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #308: the OpenShift overlay's cluster CA bundle reaches every Inkcast Deployment, including the ones
components/split-services makes when an operator adds it to the overlay (docs/epics/E1-foundations/implementation.md:
"To split, add components/split-services to an overlay")."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CA_FILE = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"


class SplitServicesOnOpenShift(unittest.TestCase):
    def test_every_inkcast_deployment_trusts_the_injected_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            kustomize = pathlib.Path(tmp) / "kustomize"
            shutil.copytree(ROOT / "deploy" / "kustomize", kustomize)
            overlay = kustomize / "overlays" / "openshift" / "kustomization.yaml"
            text = overlay.read_text()
            self.assertIn("components:\n", text)
            overlay.write_text(text.replace("components:\n", "components:\n  - ../../components/split-services\n", 1))
            out = subprocess.run(["kubectl", "kustomize", str(overlay.parent)], capture_output=True, text=True,
                                 check=True)
        deployments = [d for d in yaml.safe_load_all(out.stdout) if d and d["kind"] == "Deployment"
                       and d["metadata"]["labels"].get("app.kubernetes.io/name") == "inkcast"]
        self.assertEqual(len(deployments), 6)   # one per service, the single inkcast Deployment deleted
        for d in deployments:
            with self.subTest(deployment=d["metadata"]["name"]):
                container = d["spec"]["template"]["spec"]["containers"][0]
                self.assertEqual(container["name"], "inkcast")
                self.assertIn({"name": "SSL_CERT_FILE", "value": CA_FILE}, container["env"])
                self.assertIn({"name": "trusted-ca", "mountPath": "/etc/pki/ca-trust/extracted/pem", "readOnly": True},
                              container["volumeMounts"])


if __name__ == "__main__":
    unittest.main()
