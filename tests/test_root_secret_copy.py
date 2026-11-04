# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A kept copy of each environment's root Secret (issue #436, ADR-0020), as the manifests render it: the OADP
operator pinned through OLM (deploy/kustomize/shared/oadp-operator), Velero alone with the storage location
root-secrets and one Schedule per environment selecting `secrets` by the label `inkcast env-secrets` sets
(shared/oadp; the code's ROOT_SECRET_LABEL), its TTL at least INKCAST_BACKUP_KEEP days of each environment, the
lab's stand-in store from SeaweedFS's own chart (shared/seaweedfs/values.yaml, read here as the chart reads it, and
rendered with the pinned chart when helm and the checked tgz are at hand: CI has no helm, so the values file is what
CI holds), its setup Job, its admission policy and its RBAC (shared/seaweedfs, shared/seaweedfs-admission,
shared/seaweedfs-access), the label on every manifest that defines inkcast-secrets, and the two scripts (the lab's
install, the drill) doing what the runbook says. The lab run is in the pull request; scripts/ops/root-secret-drill.sh
is the drill (tests/test_review_root_secret_436_478.py drives it against a stub)."""

from __future__ import annotations

import hashlib
import importlib.util
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
from test_openfga_component import KUSTOMIZE, one, pod_spec, render

from inkcast.env_secrets import ROOT_SECRET_LABEL

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENVIRONMENTS = {"environments/staging": "inkcast-staging", "environments/production": "inkcast-prod"}
ADP = "openshift-adp"
STORE_NS = "inkcast-seaweedfs"
BUCKET = "inkcast-root-secrets"
BSL = "root-secrets"
CREDENTIAL = "cloud-credentials-root-secrets"
IDENTITIES = "seaweedfs-s3-config"
SETUP_ACCOUNT = f"system:serviceaccount:{STORE_NS}:seaweedfs-setup"
SEAWEEDFS_IMAGE = re.compile(r"^chrislusf/seaweedfs:4\.52@sha256:[0-9a-f]{64}$")
RESTRICTED = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
INSTALL = ROOT / "scripts" / "lab" / "root-secret-copy.sh"
DRILL = ROOT / "scripts" / "ops" / "root-secret-drill.sh"
STORE = KUSTOMIZE / "shared" / "seaweedfs"
SETUP = STORE / "setup.sh"
VALUES = STORE / "values.yaml"
RUNBOOK = ROOT / "docs" / "runbooks" / "env-secrets.md"
ADR = ROOT / "docs" / "adr" / "0020-root-secret-copy-in-its-own-storage-location.md"
VERSIONS = dict(re.findall(r"^([A-Za-z0-9_]+)=(.*)$", (ROOT / "scripts" / "versions.env").read_text(), re.M))


def of_kind(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def hours(ttl: str) -> float:
    """A Velero duration such as 2160h0m0s, in hours."""
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", ttl)
    assert match and ttl, ttl
    h, m, s = (int(x or 0) for x in match.groups())
    return h + m / 60 + s / 3600


def chart_tgz() -> pathlib.Path | None:
    """The pinned chart, when a copy is at hand: SEAWEEDFS_CHART_TGZ, or ~/.cache/inkcast/charts/seaweedfs-<version>.tgz
    (where scripts/lab/root-secret-copy.sh's download can be kept). Only a copy whose SHA-256 is the pinned one."""
    version = VERSIONS["SEAWEEDFS_CHART_VERSION"]
    for candidate in (os.environ.get("SEAWEEDFS_CHART_TGZ"),
                      pathlib.Path.home() / ".cache" / "inkcast" / "charts" / f"seaweedfs-{version}.tgz"):
        path = pathlib.Path(candidate) if candidate else None
        if path and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == VERSIONS["SEAWEEDFS_CHART_SHA256"]:
            return path
    return None


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {name: render(name) for name in (*ENVIRONMENTS, "shared/oadp-operator", "shared/oadp",
                                                     "shared/seaweedfs", "shared/seaweedfs-access", "shared/seaweedfs-admission",
                                                     "overlays/ci", "overlays/ci-split", "overlays/ci-openfga")}

    def test_the_operator_is_pinned_through_olm_in_its_own_namespace(self):
        docs = self.docs["shared/oadp-operator"]
        self.assertEqual(sorted(d["kind"] for d in docs), ["Namespace", "OperatorGroup", "Subscription"])
        namespace = one(docs, "Namespace", ADP)
        self.assertNotIn("namespace", namespace["metadata"], "cluster-scoped: never stamped")
        self.assertEqual(namespace["metadata"]["labels"]["pod-security.kubernetes.io/warn"], "restricted")
        self.assertNotIn("pod-security.kubernetes.io/enforce", namespace["metadata"]["labels"],
                         "the operator's pods are its own to declare")
        group = one(docs, "OperatorGroup", ADP)
        self.assertEqual((group["metadata"]["namespace"], group["spec"]["targetNamespaces"]), (ADP, [ADP]), "OwnNamespace")
        sub = one(docs, "Subscription", "redhat-oadp-operator")["spec"]
        self.assertEqual(sub["installPlanApproval"], "Manual", "a catalog's move never installs another version")
        self.assertRegex(sub["startingCSV"], r"^oadp-operator\.v\d+\.\d+\.\d+$")
        self.assertEqual((sub["name"], sub["source"], sub["sourceNamespace"], sub["channel"]),
                         ("redhat-oadp-operator", "redhat-operators", "openshift-marketplace", "stable"))
        self.assertIn("startingCSV", INSTALL.read_text(), "the install script approves the plan carrying this CSV")

    def test_velero_alone_copies_into_a_storage_location_of_its_own(self):
        docs = self.docs["shared/oadp"]
        dpa = one(docs, "DataProtectionApplication", "inkcast")
        self.assertEqual(dpa["metadata"]["namespace"], ADP)
        spec = dpa["spec"]
        self.assertIs(spec["backupImages"], False, "no registry: this copies a Secret")
        self.assertIs(spec["configuration"]["nodeAgent"]["enable"], False, "no DaemonSet: no volume backups")
        self.assertEqual(spec["configuration"]["nodeAgent"]["uploaderType"], "kopia", "restic is refused by the operator")
        self.assertEqual(spec["configuration"]["velero"]["defaultPlugins"], ["openshift", "aws"])
        self.assertIn("memory", spec["configuration"]["velero"]["podConfig"]["resourceAllocations"]["requests"])
        self.assertNotIn("snapshotLocations", spec)
        (location,) = spec["backupLocations"]
        self.assertEqual(location["name"], BSL)
        velero = location["velero"]
        self.assertIs(velero["default"], True, "the operator demands a default of the one location (measured on 1.6.2)")
        self.assertEqual(velero["provider"], "aws")
        self.assertEqual(velero["credential"], {"name": CREDENTIAL, "key": "cloud"})
        self.assertEqual(velero["objectStorage"]["bucket"], BUCKET)
        self.assertEqual(velero["config"]["s3ForcePathStyle"], "true")
        self.assertEqual(velero["config"]["s3Url"], f"http://seaweedfs-all-in-one.{STORE_NS}.svc:8333",
                         "the chart's all-in-one Service, on the S3 port")
        self.assertFalse([d for d in docs if d["kind"] == "Secret"], "no credential in Git")

    def test_each_environment_s_schedule_selects_its_root_secret_and_nothing_else(self):
        schedules = of_kind(self.docs["shared/oadp"], "Schedule")
        self.assertEqual(set(schedules), {"inkcast-staging-root-secret", "inkcast-prod-root-secret"})
        for name, namespace in ENVIRONMENTS.items():
            side = namespace.removeprefix("inkcast-")
            with self.subTest(environment=name):
                schedule = schedules[f"{namespace}-root-secret"]
                self.assertEqual(schedule["metadata"]["namespace"], ADP, "a Schedule lives where Velero runs")
                self.assertEqual(schedule["metadata"]["labels"]["inkcast.io/environment"], side)
                spec = schedule["spec"]
                self.assertIs(spec["useOwnerReferencesInBackup"], False, "a deleted Schedule must not take its backups")
                self.assertRegex(spec["schedule"], r"^\d+ \d+ \* \* \*$", "daily")
                template = spec["template"]
                self.assertEqual(template["includedNamespaces"], [namespace])
                self.assertEqual(template["includedResources"], ["secrets"])
                self.assertEqual(template["labelSelector"], {"matchLabels": ROOT_SECRET_LABEL}, "the code's label, exactly")
                self.assertIs(template["includeClusterResources"], False)
                self.assertIs(template["snapshotVolumes"], False)
                self.assertIs(template["defaultVolumesToFsBackup"], False)
                self.assertEqual(template["storageLocation"], BSL, "never the default by omission: the key's bucket is named")
                # At least as long as the oldest SQLite backup the key must open: INKCAST_BACKUP_KEEP days, read
                # from the environment itself (the server and the backup CronJob read the same ConfigMap).
                keep = int(one(self.docs[name], "ConfigMap", "inkcast-config")["data"]["INKCAST_BACKUP_KEEP"])
                self.assertGreaterEqual(hours(template["ttl"]), keep * 24, f"ttl {template['ttl']} under {keep} days")
                self.assertEqual(hours(template["ttl"]), 90 * 24, "the stated value: six times the lab's 14 days")

    def test_the_kustomization_holds_what_the_chart_does_not_and_nothing_the_chart_does(self):
        docs = self.docs["shared/seaweedfs"]
        self.assertEqual(sorted(d["kind"] for d in docs), ["ConfigMap", "Job", "Namespace", "Role", "RoleBinding", "ServiceAccount"],
                         "no Deployment, claim, Service or policy by hand: those are the chart's")
        self.assertEqual(one(docs, "Namespace", STORE_NS)["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")
        self.assertFalse([d for d in docs if d["kind"] == "Secret"], "the credential is generated in the cluster")

    def test_the_setup_job_generates_the_identity_only_with_the_least_rights(self):
        docs = self.docs["shared/seaweedfs"]
        job = one(docs, "Job", "seaweedfs-setup")
        pod = pod_spec(job)
        (container,) = pod["containers"]
        self.assertRegex(container["image"], SEAWEEDFS_IMAGE, "the store's own image: sh and curl")
        self.assertEqual(container["command"], ["/bin/sh", "/opt/inkcast/setup.sh"], "not the image's entrypoint")
        self.assertEqual(container["securityContext"], RESTRICTED)
        self.assertFalse({"envFrom"} & set(container), "no value from its environment")
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertEqual(pod["serviceAccountName"], "seaweedfs-setup")
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["tmp"]["emptyDir"]["medium"], "Memory", "values pass through memory, never a disk")
        self.assertEqual(volumes["api-token"]["projected"]["sources"][0]["serviceAccountToken"]["expirationSeconds"], 600)
        self.assertEqual(one(docs, "ConfigMap", "seaweedfs-setup")["data"]["setup.sh"], SETUP.read_text())
        role = one(docs, "Role", "seaweedfs-setup")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": [IDENTITIES], "verbs": ["get"]},
                                         {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]}], "never update or patch")
        access = self.docs["shared/seaweedfs-access"]
        role = one(access, "Role", "seaweedfs-setup")
        self.assertEqual(role["metadata"]["namespace"], ADP)
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": [CREDENTIAL], "verbs": ["get"]},
                                         {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]}])
        self.assertEqual(one(access, "RoleBinding", "seaweedfs-setup")["subjects"],
                         [{"kind": "ServiceAccount", "name": "seaweedfs-setup", "namespace": STORE_NS}])
        script = SETUP.read_text()
        for needed in (f"store_secret={IDENTITIES}", "store_key=seaweedfs_s3_config", f"velero_secret={CREDENTIAL}",
                       f"bucket={BUCKET}", "/dev/urandom", '-H "@$work/auth"', "aws_access_key_id", "disagree"):
            self.assertIn(needed, script)
        for gone in ("weed shell", "s3.bucket.list", "cluster/status"):
            self.assertNotIn(gone, script, "the Job touches the store not at all: the chart makes the bucket")
        self.assertNotIn("echo \"$key\"", script)
        self.assertNotIn("set -x", script)

    def test_the_setup_account_creates_opaque_secrets_under_its_two_names_only(self):
        """Review concern C5 of PR #478: with `create` on secrets in openshift-adp and `get` on the credential by
        name, the account could create a service-account-token Secret under that name annotated for Velero's
        account (`*` on everything, the privileged SCC), let the token controller fill it, and read it. The policy
        closes that: Opaque only, the two names only, no service-account annotation; and the install script proves
        the denial with server-side dry runs."""
        docs = self.docs["shared/seaweedfs-admission"]
        policy = one(docs, "ValidatingAdmissionPolicy", "seaweedfs-setup-creates-its-own")
        self.assertNotIn("namespace", policy["metadata"], "cluster-scoped: never stamped")
        spec = policy["spec"]
        self.assertEqual(spec["failurePolicy"], "Fail")
        self.assertEqual(spec["matchConstraints"]["resourceRules"],
                         [{"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE"], "resources": ["secrets"]}])
        (condition,) = spec["matchConditions"]
        self.assertIn(f"'{SETUP_ACCOUNT}'", condition["expression"])
        expressions = "\n".join(v["expression"] for v in spec["validations"])
        self.assertIn("object.type == 'Opaque'", expressions)
        self.assertIn(f"request.namespace == '{ADP}' && object.metadata.name == '{CREDENTIAL}'", expressions)
        self.assertIn(f"request.namespace == '{STORE_NS}' && object.metadata.name == '{IDENTITIES}'", expressions)
        self.assertIn("'kubernetes.io/service-account.name' in object.metadata.annotations", expressions)
        self.assertTrue(all(v.get("reason") == "Forbidden" for v in spec["validations"]))
        binding = one(docs, "ValidatingAdmissionPolicyBinding", "seaweedfs-setup-creates-its-own")
        self.assertEqual((binding["spec"]["policyName"], binding["spec"]["validationActions"]),
                         ("seaweedfs-setup-creates-its-own", ["Deny"]))
        text = INSTALL.read_text()
        self.assertLess(text.index('apply -k "$ROOT/deploy/kustomize/shared/seaweedfs-admission"'),
                        text.index('apply -k "$ROOT/deploy/kustomize/shared/seaweedfs-access"'), "the policy before the right")
        self.assertIn("--dry-run=server", text)
        self.assertIn("kubernetes.io/service-account-token", text)
        self.assertIn('--as="$setup_account"', text)

    def test_every_manifest_that_defines_inkcast_secrets_carries_the_label(self):
        for name in ("overlays/ci", "overlays/ci-split", "overlays/ci-openfga"):
            with self.subTest(overlay=name):
                secret = one(self.docs[name], "Secret", "inkcast-secrets")
                self.assertEqual({k: v for k, v in secret["metadata"]["labels"].items() if k in ROOT_SECRET_LABEL}, ROOT_SECRET_LABEL)
        for name in ENVIRONMENTS:
            self.assertFalse([d for d in self.docs[name] if d["kind"] == "Secret"], f"{name}: env-secrets makes it, labelled")
        dev = yaml.safe_load((ROOT / "deploy" / "dev" / "kustomization.yaml").read_text())
        (generator,) = [g for g in dev["secretGenerator"] if g["name"] == "inkcast-secrets"]
        self.assertEqual(generator["options"]["labels"], ROOT_SECRET_LABEL)
        external = yaml.safe_load((KUSTOMIZE / "components" / "external-secrets" / "externalsecret.yaml").read_text())
        template = external["spec"]["target"]["template"]
        self.assertEqual(template["metadata"]["labels"], ROOT_SECRET_LABEL)
        self.assertEqual(template["mergePolicy"], "Merge", "stated: the fetched keys stay beside the label")


class ChartValues(unittest.TestCase):
    """deploy/kustomize/shared/seaweedfs/values.yaml, the store as the chart installs it: what CI can hold without
    helm. Each check names the chart value it reads (k8s/charts/seaweedfs/values.yaml at 4.52)."""

    @classmethod
    def setUpClass(cls):
        cls.values = yaml.safe_load(VALUES.read_text())

    def test_the_chart_is_pinned_by_version_and_digest_and_the_image_by_digest(self):
        self.assertRegex(VERSIONS["SEAWEEDFS_CHART_VERSION"], r"^4\.52\.\d+$")
        self.assertRegex(VERSIONS["SEAWEEDFS_CHART_SHA256"], r"^[0-9a-f]{64}$")
        self.assertEqual(VERSIONS["SEAWEEDFS_CHART_REPO"], "https://seaweedfs.github.io/seaweedfs/helm")
        image = self.values["master"]["imageOverride"]   # the all-in-one pod and the hook take the master's image
        self.assertRegex(image, SEAWEEDFS_IMAGE, "the chart's appVersion, by digest")
        spec = importlib.util.spec_from_file_location("images", ROOT / "scripts" / "images.py")
        images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(images)
        pins = {str(p.relative_to(ROOT)): m.group(0) for p, _, m in images.references()}
        self.assertEqual(pins.get(str(VALUES.relative_to(ROOT))), image, "scripts/images.py reads the imageOverride line")
        for found in pins.values():
            if found.startswith("chrislusf/seaweedfs:"):
                self.assertEqual(found, image, "one pin, everywhere")

    def test_one_pod_one_claim_one_bucket_one_identity_everything_else_off(self):
        v = self.values
        self.assertIs(v["allInOne"]["enabled"], True)
        for component in ("master", "volume", "filer", "s3", "sftp", "admin", "worker", "cosi"):
            self.assertIs(v[component]["enabled"], False, f"{component}: off, allInOne does not turn it off")
        self.assertEqual(v["allInOne"]["updateStrategy"], {"type": "Recreate"})
        self.assertEqual(v["allInOne"]["replicas"], 1)
        self.assertEqual(v["allInOne"]["data"], {"type": "persistentVolumeClaim", "size": "2Gi"})
        s3 = v["allInOne"]["s3"]
        self.assertIs(s3["enabled"], True)
        self.assertIs(s3["enableAuth"], True, "without auth every request would be anonymous")
        self.assertEqual(s3["existingConfigSecret"], IDENTITIES, "the Secret the setup Job writes, key seaweedfs_s3_config")
        self.assertEqual(s3["createBuckets"], [{"name": BUCKET}])
        self.assertEqual(set(v["allInOne"]["extraArgs"]), {"-s3.port.iceberg=0", "-s3.port.lance=0", "-master.telemetry=false"})
        self.assertEqual(v["allInOne"]["serviceAccountName"], "seaweedfs")
        self.assertIs(v["global"]["seaweedfs"]["automountServiceAccountToken"], False)
        self.assertIs(v["global"]["seaweedfs"]["createClusterRole"], False)
        self.assertIsNone(v["allInOne"]["resources"]["limits"]["cpu"], "null clears the chart's CPU limit from the merge")

    def test_the_restricted_scc_s_contexts_from_the_chart_s_own_openshift_values(self):
        pod = self.values["allInOne"]["podSecurityContext"]
        self.assertEqual(pod, {"enabled": True, "runAsNonRoot": True}, "no runAsUser: the namespace's range")
        container = self.values["allInOne"]["containerSecurityContext"]
        self.assertEqual(container, {"enabled": True, "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}, **RESTRICTED})

    def test_only_velero_reaches_the_store(self):
        policy = self.values["networkPolicy"]
        self.assertIs(policy["enabled"], True)
        self.assertNotIn("extraIngress", policy, "a global rule would reach every component's policy")
        self.assertFalse(policy.get("egress", {}).get("enabled"), "egress stays unrestricted: no default-deny here")
        (rule,) = policy["components"]["seaweedfs-all-in-one"]["extraIngress"]
        self.assertEqual(rule["from"], [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ADP}},
                                        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "velero"}}}])
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 8333}], "the S3 port alone")


@unittest.skipUnless(shutil.which("helm") and chart_tgz(), "helm and the pinned chart (SEAWEEDFS_CHART_TGZ, or "
                     "~/.cache/inkcast/charts/seaweedfs-<version>.tgz with the pinned SHA-256) render it; CI has neither")
class ChartRendered(unittest.TestCase):
    """The pinned chart rendered with the values file, as `helm upgrade --install` renders it on the lab."""

    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["helm", "template", "seaweedfs", str(chart_tgz()), "-n", STORE_NS, "-f", str(VALUES)],
                             capture_output=True, text=True, check=True)
        cls.docs = [d for d in yaml.safe_load_all(out.stdout) if d]

    def test_the_all_in_one_pod_serves_s3_with_the_job_s_identities_restricted(self):
        deploy = one(self.docs, "Deployment", "seaweedfs-all-in-one")
        pod = pod_spec(deploy)
        (container,) = pod["containers"]
        self.assertRegex(container["image"], SEAWEEDFS_IMAGE)
        command = container["command"][2]
        for arg in ("-s3 ", "-s3.port=8333", "-s3.config=/etc/sw/s3/seaweedfs_s3_config", "-s3.port.iceberg=0", "-s3.port.lance=0",
                    "-master.telemetry=false"):
            self.assertIn(arg, command)
        self.assertNotIn("-webdav", command)
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["config-s3-users"]["secret"]["secretName"], IDENTITIES)
        self.assertEqual(volumes["data"]["persistentVolumeClaim"]["claimName"], "seaweedfs-all-in-one-data")
        self.assertEqual(container["securityContext"], {**RESTRICTED, "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}})
        self.assertEqual(pod["securityContext"], {"runAsNonRoot": True})
        self.assertEqual(pod["serviceAccountName"], "seaweedfs")
        self.assertEqual(container["resources"]["limits"], {"memory": "512Mi"}, "no CPU limit")
        self.assertEqual(deploy["spec"]["strategy"], {"type": "Recreate"})
        self.assertEqual(one(self.docs, "ServiceAccount", "seaweedfs")["automountServiceAccountToken"], False)
        self.assertEqual(one(self.docs, "PersistentVolumeClaim", "seaweedfs-all-in-one-data")["spec"]["resources"]["requests"]["storage"], "2Gi")
        self.assertIn({"name": "swfs-s3", "port": 8333, "targetPort": 8333, "protocol": "TCP"},
                      one(self.docs, "Service", "seaweedfs-all-in-one")["spec"]["ports"])
        self.assertFalse([d for d in self.docs if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-s3-secret")],
                         "with existingConfigSecret the chart generates no S3 identities of its own")
        self.assertFalse([d for d in self.docs if d["kind"] in ("ClusterRole", "ClusterRoleBinding")])

    def test_the_policy_admits_the_release_and_velero_s_pods_on_the_s3_port_and_nothing_else(self):
        policy = one(self.docs, "NetworkPolicy", "seaweedfs-all-in-one")["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        release, velero = policy["ingress"]
        self.assertEqual(release["from"], [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "seaweedfs",
                                                                           "app.kubernetes.io/instance": "seaweedfs"}}}])
        self.assertEqual(velero["from"], [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ADP}},
                                          "podSelector": {"matchLabels": {"app.kubernetes.io/name": "velero"}}}])
        self.assertEqual(velero["ports"], [{"port": 8333, "protocol": "TCP"}])

    def test_the_bucket_hook_makes_the_bucket_with_the_job_s_identities_mounted(self):
        hook = one(self.docs, "Job", "seaweedfs-bucket-hook")
        self.assertEqual(hook["metadata"]["annotations"]["helm.sh/hook"], "post-install,post-upgrade")
        pod = pod_spec(hook)
        (container,) = pod["containers"]
        self.assertIn(f"s3.bucket.create --name {BUCKET}", container["command"][2])
        self.assertEqual({v["name"]: v.get("secret", {}).get("secretName") for v in pod["volumes"] if "secret" in v},
                         {"config-users": IDENTITIES}, "the Secret exists before the install: the Job runs first")
        self.assertEqual(container["securityContext"]["readOnlyRootFilesystem"], True)


class Scripts(unittest.TestCase):
    """The lab's install and the drill, read: they do what the runbook says, in order, and never print a value."""

    def test_the_install_applies_in_order_and_proves_a_backup_in_the_bucket(self):
        text = INSTALL.read_text()
        order = [text.index(f'apply -k "$ROOT/deploy/kustomize/shared/{d}"')
                 for d in ("oadp-operator", "seaweedfs-admission", "seaweedfs-access", "seaweedfs")]
        order += [text.index('helm upgrade --install "$release"'), text.index('apply -k "$ROOT/deploy/kustomize/shared/oadp"')]
        self.assertEqual(order, sorted(order), "operator, the policy, the right, the Job, the chart, then the DPA")
        for needed in ("installplan", '{"spec":{"approved":true}}', "Succeeded", "velero.io/schedule-name",
                       "status.progress.itemsBackedUp", "Available", "Reconciled", "weed shell", "velero-backup.json",
                       "label secret inkcast-secrets inkcast.io/root-secret=true", 'source "$ROOT/scripts/versions.env"',
                       "SEAWEEDFS_CHART_SHA256", '--version "$SEAWEEDFS_CHART_VERSION"', "values.yaml", "--wait"):
            self.assertIn(needed, text)
        self.assertNotIn("delete secret", text, "the install never deletes a Secret")
        self.assertNotIn("delete namespace", text)
        self.assertNotIn("delete pvc", text)
        self.assertNotIn("helm uninstall", text)

    def test_the_install_approves_only_a_plan_of_the_pinned_csv_alone(self):
        """Review concern C6 of PR #478: an InstallPlan can carry several CSVs; approving one that carries the
        pinned version beside another installs what the pin never named. The selector, run as the script runs it."""
        code = re.search(r"plan_for_csv\(\) \{\n.*?\"\$py\" -c '(.*?)' \"\$csv\"\n\}", INSTALL.read_text(), re.S).group(1)

        def select(plans: list[list[str]]) -> subprocess.CompletedProcess:
            items = [{"metadata": {"name": f"install-{i}"}, "spec": {"clusterServiceVersionNames": names}} for i, names in enumerate(plans)]
            return subprocess.run([sys.executable, "-c", code, "oadp-operator.v1.6.2"], input=json.dumps({"items": items}),
                                  capture_output=True, text=True)
        exact = select([["oadp-operator.v1.6.2"]])
        self.assertEqual((exact.returncode, exact.stdout.strip()), (0, "install-0"))
        mixed = select([["oadp-operator.v1.6.2", "something-else.v9"]])
        self.assertNotEqual(mixed.returncode, 0, "a plan carrying the pinned CSV beside another stops the script")
        self.assertIn("install-0", mixed.stderr)
        self.assertIn("something-else.v9", mixed.stderr)
        self.assertEqual(mixed.stdout, "")
        other = select([["oadp-operator.v1.7.0"]])
        self.assertEqual((other.returncode, other.stdout), (0, ""), "another version is not this plan: wait")
        later = select([["oadp-operator.v1.7.0"], ["oadp-operator.v1.6.2"]])
        self.assertEqual(later.stdout.strip(), "install-1")

    def test_the_drill_deletes_one_secret_restores_by_label_and_compares_by_hash(self):
        text = DRILL.read_text()
        commands = [line for line in text.splitlines() if "delete secret" in line and not line.lstrip().startswith("#")]
        self.assertEqual(commands, ['$k -n "$ns" delete secret "$secret"'], "the one destructive step, and the only Secret")
        self.assertIn("secret=inkcast-secrets", text)
        steps = [text.index(f'step "{s}') for s in ("1. the safety copy", "2. a backup from Schedule", "3. THE LOSS",
                                                   "4. the restore, limited to secrets", "5. the Secret is back",
                                                   "6. the CronJob's run now", "7. sealed credentials still read")]
        self.assertEqual(steps, sorted(steps))
        self.assertLess(text.index("umask 077"), steps[0], "the copy's mode is set before anything is written")
        self.assertLess(text.index("\ndeleted=yes"), text.index('$k -n "$ns" delete secret "$secret"'),
                        "marked before the command: a delete whose answer never arrives still puts the copy back")
        for needed in ("trap finish EXIT", "put-back.json", "hashlib.sha256", ".hexdigest()[:12]", '"existingResourcePolicy": "none"',
                       '"includedResources": ["secrets"]', "labelSelector", "every generated Secret is present and in agreement.",
                       "from inkcast.libs.secrets import Sealer", "INKCAST_SECRET_KEY_FILE", "SELECT sealed FROM credentials",
                       "--from=cronjob/inkcast-env-secrets"):
            self.assertIn(needed, text)
        self.assertNotIn("set -x", text)
        self.assertNotIn("base64 -d", text, "a value is never decoded on the host")
        self.assertNotIn("cat \"$work/copy.json\"", text)

    def test_the_scripts_and_the_setup_script_parse(self):
        for path, shell in ((INSTALL, "bash"), (DRILL, "bash"), (SETUP, "sh")):
            with self.subTest(script=path.name):
                subprocess.run([shell, "-n", str(path)], check=True)
                self.assertTrue(path.read_text().startswith(f"#!/{'usr/bin/env bash' if shell == 'bash' else 'bin/sh'}\n# SPDX-License-Identifier"))
        self.assertTrue(INSTALL.stat().st_mode & 0o111, "executable")
        self.assertTrue(DRILL.stat().st_mode & 0o111, "executable")

    def test_the_scripts_run_python_from_the_checkout_s_venv_and_stop_without_it(self):
        """The host's python3 is not the project's (macOS ships 3.9): every call goes through the checkout's .venv,
        or INKCAST_PYTHON, and a missing interpreter stops the script before it calls the cluster."""
        for path, args in ((INSTALL, []), (DRILL, ["inkcast-staging"])):
            with self.subTest(script=path.name):
                self.assertIsNone(re.search(r"(?m)^[^#]*\bpython3 ", path.read_text()), "a bare python3")
                calls = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory())) / "calls"
                stub = calls.with_name("oc")
                stub.write_text(f"#!/bin/sh\necho \"$*\" >> {calls}\n")
                stub.chmod(0o755)
                done = subprocess.run(["bash", str(path), *args], capture_output=True, text=True, timeout=30,
                                      env={**os.environ, "KUBECTL": str(stub), "INKCAST_PYTHON": "/nonexistent/python"})
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("no Python at /nonexistent/python", done.stderr)
                self.assertFalse(calls.exists(), "the cluster was called before the interpreter was checked")


class Documented(unittest.TestCase):
    def test_the_runbook_names_the_copy_the_latest_backup_the_restore_and_the_check(self):
        text = RUNBOOK.read_text()
        for needed in ("inkcast-<side>-root-secret", "root-secrets", "velero.io/schedule-name", "kind: Restore",
                       "velero restore create --from-backup", "--selector inkcast.io/root-secret=true", "--include-resources secrets",
                       "scripts/ops/root-secret-drill.sh", "every generated Secret is present and in agreement",
                       "same cluster", "ADR-0020",
                       # Review concerns C3 and C9 of PR #478: the only location is the default; a new Secret made meanwhile.
                       "without `storageLocation`", "rollout restart deploy/openfga deploy/inkcast"):
            self.assertIn(needed, text)
        backup_restore = (ROOT / "docs" / "runbooks" / "backup-restore.md").read_text()
        self.assertIn("env-secrets.md", backup_restore.split("## Off-cluster copies", 1)[1])
        self.assertIn("never", backup_restore.split("## Off-cluster copies", 1)[1], "the key apart from the data")
        self.assertEqual(text.count("same cluster"), 1, "the lab store's limit, stated once")

    def test_the_adr_is_listed_and_cites_its_sources(self):
        text = ADR.read_text()
        for needed in ("docs.percona.com", "velero.io/docs/main/restore-reference", "velero.io/docs/main/resource-filtering",
                       "docs.okd.io", "external-secrets.io", "SeaweedFS", "MinIO", "accepted", "Helm chart", "openshift-values.yaml",
                       "4.52.0", "RustFS", "charts.rustfs.com", "runAsUser", "rustfs/rustfs#2063", "without `storageLocation`",
                       "a change of the store's values"):
            self.assertIn(needed, text)
        index = (ROOT / "docs" / "adr" / "README.md").read_text()
        self.assertIn(f"[0020]({ADR.name})", index)


if __name__ == "__main__":
    unittest.main()
