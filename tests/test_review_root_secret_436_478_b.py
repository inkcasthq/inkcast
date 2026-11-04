# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #478 (issue #436), second pass over a124d123..74614060: the guards the rework added, held by tests
that fail when a guard stops guarding. No cluster is reached: the scripts' own blocks run against stub commands."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "deploy" / "kustomize" / "shared" / "seaweedfs-admission" / "policy.yaml"
INSTALL = ROOT / "scripts" / "lab" / "root-secret-copy.sh"
DPA = ROOT / "deploy" / "kustomize" / "shared" / "oadp" / "dpa.yaml"
ADR = ROOT / "docs" / "adr" / "0020-root-secret-copy-in-its-own-storage-location.md"
RUNBOOK = ROOT / "docs" / "runbooks" / "env-secrets.md"
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


def until(first: str, stop: str) -> str:
    """The install script's lines from the one starting with `first` up to (not including) the line `stop`."""
    match = re.search(rf"^{re.escape(first)}.*?(?=^{re.escape(stop)})", INSTALL.read_text(), re.M | re.S)
    assert match, f"no block from {first!r} to {stop!r} in {INSTALL}"
    return match.group(0)


class Stubbed(unittest.TestCase):
    """A directory on PATH whose commands are stubs, and a bash that runs part of the install script with them."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="r478b-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "bin").mkdir()

    def stub(self, name: str, body: str) -> None:
        path = self.dir / "bin" / name
        path.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n" + body)
        path.chmod(0o755)

    def bash(self, script: str, env: dict[str, str], timeout: float = 30) -> subprocess.CompletedProcess:
        env = {**os.environ, **env, "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "STUB_DIR": str(self.dir), "INKCAST_PYTHON": sys.executable}
        return subprocess.run(["bash", "-c", "set -euo pipefail\npy=\"$INKCAST_PYTHON\"\n" + script], capture_output=True, text=True, env=env,
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



# The stub `oc` for the admission probes: a dry-run create answered as the policy answers it, less the one rule named
# by $POLICY_LACKS ("type", "name", "annotation"), or refusing the credential itself ("credential"); "" is the policy.
PROBE_STUB = """\
with open(pathlib.Path(os.environ["STUB_DIR"], "calls.log"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
secret, lacks = json.load(sys.stdin), os.environ["POLICY_LACKS"]
meta = secret["metadata"]
where = meta["namespace"] + "/" + meta["name"]
refusal = ""
if lacks != "type" and secret["type"] != "Opaque":
    refusal = "seaweedfs-setup creates Opaque Secrets only, not type " + secret["type"]
elif lacks != "name" and where not in ("openshift-adp/cloud-credentials-root-secrets", "inkcast-seaweedfs/seaweedfs-s3-config"):
    refusal = "seaweedfs-setup creates only openshift-adp/cloud-credentials-root-secrets and inkcast-seaweedfs/seaweedfs-s3-config, not " + where
elif lacks != "annotation" and "kubernetes.io/service-account.name" in meta.get("annotations", {}):
    refusal = "seaweedfs-setup creates no Secret annotated for a service account"
elif lacks == "credential":
    refusal = "seaweedfs-setup creates only nothing, not " + where
if refusal:
    sys.exit('Error from server (Forbidden): error when creating "STDIN": secrets "' + meta["name"] + '" is forbidden: '
             "ValidatingAdmissionPolicy 'seaweedfs-setup-creates-its-own' with binding 'seaweedfs-setup-creates-its-own' "
             "denied request: " + refusal)
print("secret/" + meta["name"] + " created (server dry run)")
"""


class AdmissionProbes(Stubbed):
    """Step 3's server-side dry runs, run as the script runs them with `oc` stubbed: the lab's proof that the policy is
    in force. Each case removes one rule from the stub's policy; the probes must stop the script naming the hole.
    Without this, a probe whose refusal arm lost its `exit 1` passed every test (measured)."""

    def probes(self, lacks: str) -> subprocess.CompletedProcess:
        self.stub("oc", PROBE_STUB)
        script = "\n".join(['k=oc; adp=openshift-adp; store_ns=inkcast-seaweedfs',
                            'setup_account="system:serviceaccount:$store_ns:seaweedfs-setup"',
                            until("probe() {", 'step "4.')])
        return self.bash(script, {"POLICY_LACKS": lacks})

    def test_the_policy_in_force_passes_the_four_probes_each_as_the_setup_account(self):
        done = self.probes("")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len([line for line in done.stdout.splitlines() if line.startswith("admission: ")]), 4, done.stdout)
        calls = [line for line in (self.dir / "calls.log").read_text().splitlines()]
        self.assertEqual(len(calls), 4)
        for call in calls:
            self.assertIn('"--dry-run=server"', call)
            self.assertIn(f'"--as={SETUP_ACCOUNT}"', call)

    def test_each_hole_in_the_policy_stops_the_script_naming_it(self):
        for lacks, named in (("type", "a service-account-token Secret as"), ("name", "another name as"),
                             ("annotation", "the annotated credential as"), ("credential", "under its own name was denied")):
            with self.subTest(policy_lacks=lacks):
                done = self.probes(lacks)
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn(named, done.stderr)



# The stub `oc` for step 1: $PLANS (JSON) is the namespace's InstallPlans; a patch is logged, nothing else changes.
PLAN_STUB = """\
args = sys.argv[1:]
with open(pathlib.Path(os.environ["STUB_DIR"], "calls.log"), "a") as log:
    log.write(json.dumps(args) + "\\n")
plans = json.loads(os.environ["PLANS"])
if "patch" in args:
    sys.exit(0)
answered = pathlib.Path(os.environ["STUB_DIR"], "answered")
if args[-2:] == ["-o", "json"] and os.environ.get("FIRST_LIST_FAILS") and not answered.exists():
    answered.touch()
    sys.exit("Error from server: the server is currently unable to handle the request")
if args[-2:] == ["-o", "json"]:
    print(json.dumps({"items": plans}))
elif any(a.startswith("installplan/") for a in args):
    (plan,) = [p for p in plans if "installplan/" + p["metadata"]["name"] in args]
    print("true" if plan["spec"].get("approved") else "false", end="")
else:
    sys.exit(1)
"""


class InstallPlanApproval(Stubbed):
    """Step 1's approval, run as the script runs it with `oc` stubbed. A plan that carries the pinned CSV beside
    another is named and stops the script (the script's own words): measured before the fix, the refusal ran inside
    `has_plan`'s command substitution, which `set -e` ignores in a condition, so the script waited out its 300 s
    printing the refusal every 5 s before it stopped on the timeout."""

    def approve(self, plans: list[list[str]], first_list_fails: bool = False) -> subprocess.CompletedProcess:
        self.stub("oc", PLAN_STUB)
        items = [{"metadata": {"name": f"install-{i}"}, "spec": {"clusterServiceVersionNames": names, "approved": False}}
                 for i, names in enumerate(plans)]
        script = "\n".join([function("field"), function("wait_for"), 'k=oc; adp=openshift-adp; csv=oadp-operator.v1.6.2',
                            until("plan_for_csv() {", "csv_succeeded() {")])
        try:
            return self.bash(script, {"PLANS": json.dumps(items), "FIRST_LIST_FAILS": "1" if first_list_fails else ""},
                             timeout=20)
        except subprocess.TimeoutExpired as e:
            self.fail(f"still waiting after {e.timeout} s on a plan it refuses: {e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr}")

    def patches(self) -> list[str]:
        return [line for line in (self.dir / "calls.log").read_text().splitlines() if '"patch"' in line]

    def test_a_plan_carrying_another_csv_stops_the_script_at_once_naming_it(self):
        done = self.approve([["oadp-operator.v1.6.2", "something-else.v9"]])
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertEqual(done.stderr.count("not approved"), 1, done.stderr)
        self.assertIn("install-0", done.stderr)
        self.assertEqual(self.patches(), [], "a plan carrying another CSV was approved")

    def test_the_plan_of_the_pinned_csv_alone_is_approved(self):
        done = self.approve([["oadp-operator.v1.7.0"], ["oadp-operator.v1.6.2"]])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("InstallPlan install-1 approved (oadp-operator.v1.6.2 alone)", done.stdout)
        (patch,) = self.patches()
        self.assertIn('"install-1"', patch)

    def test_a_failed_listing_is_waited_through_not_taken_for_a_refusal(self):
        done = self.approve([["oadp-operator.v1.6.2"]], first_list_fails=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("InstallPlan install-0 approved", done.stdout)



class DefaultLocationRule(unittest.TestCase):
    def test_the_documents_say_what_the_dpa_holds_about_the_default_location(self):
        """The rule (no Velero Backup or Schedule without `storageLocation` while `root-secrets` is the default) is
        stated where a reader looks. The DPA is shared by every cluster and holds one location; while it does, no
        document may say production already ships another as the default: E7.F7.6.S2, which adds it, is not done
        (dpa.yaml: "When E7.F7.6.S2 adds a location ..."; docs/runbooks/backup-restore.md: it "covers" replication)."""
        locations = yaml.safe_load(DPA.read_text())["spec"]["backupLocations"]
        for path in (ADR, RUNBOOK, DPA):
            text = " ".join(path.read_text().split())
            with self.subTest(document=path.name):
                self.assertIn("never create a Velero Backup or Schedule", text)
                self.assertIn("`storageLocation`", text)
                if len(locations) == 1:
                    found = re.search(r"(?i)production ships the data backups' location", text)
                    self.assertIsNone(found, "a second location is stated as shipped while the DPA holds one")


if __name__ == "__main__":
    unittest.main()
