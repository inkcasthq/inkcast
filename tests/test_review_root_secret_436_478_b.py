# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #478 (issue #436), second pass over a124d123..74614060: the guards the rework added, held by tests
that fail when a guard stops guarding. No cluster is reached: the scripts' own blocks run against stub commands."""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "deploy" / "kustomize" / "shared" / "seaweedfs-admission" / "policy.yaml"
INSTALL = ROOT / "scripts" / "lab" / "root-secret-copy.sh"
SETUP_ACCOUNT = "system:serviceaccount:inkcast-seaweedfs:seaweedfs-setup"


def function(name: str) -> str:
    """One shell function of the install script, as the script defines it (one line, or up to a closing brace)."""
    match = re.search(rf"^{name}\(\) \{{(?:[^\n]*\}}$|\n.*?^\}}$)", INSTALL.read_text(), re.M | re.S)
    assert match, f"{name}() is not in {INSTALL}"
    return match.group(0)


def block(first: str, last: str) -> str:
    """The install script's lines from the one starting with `first` to the next one ending with `last`."""
    match = re.search(rf"^{re.escape(first)}.*?{re.escape(last)}$", INSTALL.read_text(), re.M | re.S)
    assert match, f"no block from {first!r} to {last!r} in {INSTALL}"
    return match.group(0)


class Stubbed(unittest.TestCase):
    """A directory on PATH whose commands are stubs, and a bash that runs part of the install script with them."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="r478b-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "bin").mkdir()

    def stub(self, name: str, body: str) -> None:
        path = self.dir / "bin" / name
        path.write_text("#!/usr/bin/env python3\nimport json, os, pathlib, sys\n" + body)
        path.chmod(0o755)

    def bash(self, script: str, env: dict[str, str], timeout: float = 30) -> subprocess.CompletedProcess:
        env = {**os.environ, **env, "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "STUB_DIR": str(self.dir)}
        return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], capture_output=True, text=True, env=env,
                              timeout=timeout)


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



class ChartDigest(Stubbed):
    """Step 4 of the install, run as the script runs it with `curl` and `helm` stubbed: the downloaded tgz is checked
    against scripts/versions.env before `helm upgrade --install` sees it. Without this, removing the `exit 1` after
    the mismatch leaves every test green (measured) and an unverified chart installs."""

    def step4(self, served: bytes, pinned: str) -> subprocess.CompletedProcess:
        (self.dir / "served.tgz").write_bytes(served)
        self.stub("curl", "out = sys.argv[sys.argv.index('-o') + 1]\n"
                          "pathlib.Path(out).write_bytes(pathlib.Path(os.environ['STUB_DIR'], 'served.tgz').read_bytes())\n")
        self.stub("helm", "with open(pathlib.Path(os.environ['STUB_DIR'], 'helm.log'), 'a') as log:\n"
                          "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n")
        script = "\n".join([function("step"), function("sha256"),
                            'ROOT="$CHECKOUT"; release=seaweedfs; store_ns=inkcast-seaweedfs; work="$STUB_DIR"',
                            block('step "4.', "--wait --timeout 10m")])
        return self.bash(script, {"CHECKOUT": str(ROOT), "SEAWEEDFS_CHART_REPO": "https://charts.invalid",
                                  "SEAWEEDFS_CHART_VERSION": "4.52.0", "SEAWEEDFS_CHART_SHA256": pinned})

    def helm_calls(self) -> list[str]:
        log = self.dir / "helm.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_a_tgz_other_than_the_pinned_one_stops_the_script_before_helm(self):
        done = self.step4(b"not the chart", hashlib.sha256(b"the chart").hexdigest())
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("is not the pinned", done.stderr)
        self.assertIn("not installed", done.stderr)
        self.assertEqual(self.helm_calls(), [], "helm ran on a chart whose SHA-256 is not the pinned one")

    def test_the_pinned_tgz_is_the_one_helm_installs(self):
        done = self.step4(b"the chart", hashlib.sha256(b"the chart").hexdigest())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("SHA-256 as pinned", done.stdout)
        (call,) = self.helm_calls()
        self.assertIn(str(self.dir / "seaweedfs-4.52.0.tgz"), call)
        self.assertIn(str(ROOT / "deploy" / "kustomize" / "shared" / "seaweedfs" / "values.yaml"), call)


if __name__ == "__main__":
    unittest.main()
