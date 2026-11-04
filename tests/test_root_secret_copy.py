# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A kept copy of each environment's root Secret (issue #436, ADR-0020), as the manifests render it: the OADP
operator pinned through OLM (deploy/kustomize/shared/oadp-operator), Velero alone with the storage location
root-secrets and one Schedule per environment selecting `secrets` by the label `inkcast env-secrets` sets
(shared/oadp; the code's ROOT_SECRET_LABEL), its TTL at least INKCAST_BACKUP_KEEP days of each environment, the
lab's stand-in store with its generated credential and its policy (shared/seaweedfs, shared/seaweedfs-access), the
label on every manifest that defines inkcast-secrets, and the two scripts (the lab's install, the drill) doing
what the runbook says. The lab run is in the pull request; scripts/ops/root-secret-drill.sh is the drill."""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
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
SEAWEEDFS_IMAGE = re.compile(r"^chrislusf/seaweedfs:4\.52@sha256:[0-9a-f]{64}$")
RESTRICTED = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
INSTALL = ROOT / "scripts" / "lab" / "root-secret-copy.sh"
DRILL = ROOT / "scripts" / "ops" / "root-secret-drill.sh"
SETUP = KUSTOMIZE / "shared" / "seaweedfs" / "setup.sh"
RUNBOOK = ROOT / "docs" / "runbooks" / "env-secrets.md"
ADR = ROOT / "docs" / "adr" / "0020-root-secret-copy-in-its-own-storage-location.md"


def of_kind(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def hours(ttl: str) -> float:
    """A Velero duration such as 2160h0m0s, in hours."""
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", ttl)
    assert match and ttl, ttl
    h, m, s = (int(x or 0) for x in match.groups())
    return h + m / 60 + s / 3600


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {name: render(name) for name in (*ENVIRONMENTS, "shared/oadp-operator", "shared/oadp",
                                                     "shared/seaweedfs", "shared/seaweedfs-access",
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
        self.assertEqual(velero["config"]["s3Url"], f"http://seaweedfs.{STORE_NS}.svc:8333", "the lab's stand-in store")
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
                self.assertEqual(template["storageLocation"], BSL)
                # At least as long as the oldest SQLite backup the key must open: INKCAST_BACKUP_KEEP days, read
                # from the environment itself (the server and the backup CronJob read the same ConfigMap).
                keep = int(one(self.docs[name], "ConfigMap", "inkcast-config")["data"]["INKCAST_BACKUP_KEEP"])
                self.assertGreaterEqual(hours(template["ttl"]), keep * 24, f"ttl {template['ttl']} under {keep} days")
                self.assertEqual(hours(template["ttl"]), 90 * 24, "the stated value: six times the lab's 14 days")

    def test_the_lab_s_store_is_one_restricted_pod_on_its_own_volume_with_one_bucket_and_one_identity(self):
        docs = self.docs["shared/seaweedfs"]
        self.assertEqual(one(docs, "Namespace", STORE_NS)["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")
        deploy = one(docs, "Deployment", "seaweedfs")
        self.assertEqual(deploy["spec"]["strategy"], {"type": "Recreate"}, "one ReadWriteOnce volume")
        pod = pod_spec(deploy)
        (container,) = pod["containers"]
        self.assertRegex(container["image"], SEAWEEDFS_IMAGE)
        self.assertEqual(container["args"][0], "mini")
        args = set(container["args"])
        self.assertIn(f"-bucket={BUCKET}", args, "the bucket is declared, not made by a client")
        self.assertIn("-s3.config=/etc/seaweedfs/identities.json", args)
        for off in ("-webdav=false", "-admin.ui=false", "-s3.port.iceberg=0", "-s3.port.lance=0", "-master.telemetry=false"):
            self.assertIn(off, args, "everything that is not S3 is off")
        self.assertEqual(container["securityContext"], RESTRICTED)
        self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}})
        self.assertIs(pod["automountServiceAccountToken"], False)
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["data"]["persistentVolumeClaim"]["claimName"], "seaweedfs-data")
        self.assertEqual(volumes["identities"]["secret"], {"secretName": "seaweedfs-s3-identities", "defaultMode": 0o440})
        self.assertEqual(one(docs, "PersistentVolumeClaim", "seaweedfs-data")["spec"]["accessModes"], ["ReadWriteOnce"])
        service = one(docs, "Service", "seaweedfs")
        self.assertIn({"name": "s3", "port": 8333, "targetPort": "s3"}, service["spec"]["ports"])
        self.assertFalse([d for d in docs if d["kind"] == "Secret"], "the credential is generated in the cluster")
        self.assertEqual(sorted(d["kind"] for d in docs), ["ConfigMap", "Deployment", "Job", "Namespace", "NetworkPolicy",
                                                           "PersistentVolumeClaim", "Role", "RoleBinding", "Service",
                                                           "ServiceAccount"])

    def test_only_velero_and_the_setup_job_reach_the_store(self):
        policy = one(self.docs["shared/seaweedfs"], "NetworkPolicy", "seaweedfs")["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        self.assertEqual(policy["podSelector"]["matchLabels"]["app.kubernetes.io/name"], "seaweedfs")
        velero, setup = policy["ingress"]
        self.assertEqual(velero["from"], [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ADP}},
                                          "podSelector": {"matchLabels": {"app.kubernetes.io/name": "velero"}}}])
        self.assertEqual(velero["ports"], [{"port": 8333, "protocol": "TCP"}], "the S3 port alone")
        self.assertEqual(setup["from"], [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "seaweedfs-setup",
                                                                          "app.kubernetes.io/part-of": "inkcast"}}}])
        job_labels = one(self.docs["shared/seaweedfs"], "Job", "seaweedfs-setup")["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(job_labels["app.kubernetes.io/name"], "seaweedfs-setup", "the policy's second rule admits the Job's pod")

    def test_the_setup_job_generates_the_credential_with_the_least_rights(self):
        docs = self.docs["shared/seaweedfs"]
        job = one(docs, "Job", "seaweedfs-setup")
        pod = pod_spec(job)
        (container,) = pod["containers"]
        self.assertRegex(container["image"], SEAWEEDFS_IMAGE, "the store's own image: sh, curl and weed")
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
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["seaweedfs-s3-identities"], "verbs": ["get"]},
                                         {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]}], "never update or patch")
        access = self.docs["shared/seaweedfs-access"]
        role = one(access, "Role", "seaweedfs-setup")
        self.assertEqual(role["metadata"]["namespace"], ADP)
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": [CREDENTIAL], "verbs": ["get"]},
                                         {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]}])
        self.assertEqual(one(access, "RoleBinding", "seaweedfs-setup")["subjects"],
                         [{"kind": "ServiceAccount", "name": "seaweedfs-setup", "namespace": STORE_NS}])
        script = SETUP.read_text()
        for needed in (f"store_secret={'seaweedfs-s3-identities'}", f"velero_secret={CREDENTIAL}", f"bucket={BUCKET}",
                       "/dev/urandom", '-H "@$work/auth"', "identities.json", "aws_access_key_id", "disagree", "s3.bucket.list"):
            self.assertIn(needed, script)
        self.assertNotIn("echo \"$key\"", script)
        self.assertNotIn("set -x", script)

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
        self.assertEqual(template["mergePolicy"], "Merge", "the fetched keys stay beside the label")


class Scripts(unittest.TestCase):
    """The lab's install and the drill, read: they do what the runbook says, in order, and never print a value."""

    def test_the_install_applies_in_order_and_proves_a_backup_in_the_bucket(self):
        text = INSTALL.read_text()
        order = [text.index(f'apply -k "$ROOT/deploy/kustomize/shared/{d}"') for d in ("oadp-operator", "seaweedfs-access", "seaweedfs", "oadp")]
        self.assertEqual(order, sorted(order), "operator, the Job's right, the store, then the DPA")
        for needed in ("installplan", '{"spec":{"approved":true}}', "Succeeded", "velero.io/schedule-name",
                       "status.progress.itemsBackedUp", "Available", "Reconciled", "weed shell", "velero-backup.json",
                       "label secret inkcast-secrets inkcast.io/root-secret=true"):
            self.assertIn(needed, text)
        self.assertNotIn("delete secret", text, "the install never deletes a Secret")
        self.assertNotIn("delete namespace", text)
        self.assertNotIn("delete pvc", text)

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
        for needed in ("trap 'rm -rf \"$work\"' EXIT", "hashlib.sha256", ".hexdigest()[:12]", '"existingResourcePolicy": "none"',
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


class Documented(unittest.TestCase):
    def test_the_runbook_names_the_copy_the_latest_backup_the_restore_and_the_check(self):
        text = RUNBOOK.read_text()
        for needed in ("inkcast-<side>-root-secret", "root-secrets", "velero.io/schedule-name", "kind: Restore",
                       "velero restore create --from-backup", "--selector inkcast.io/root-secret=true", "--include-resources secrets",
                       "scripts/ops/root-secret-drill.sh", "every generated Secret is present and in agreement",
                       "same cluster", "ADR-0020"):
            self.assertIn(needed, text)
        backup_restore = (ROOT / "docs" / "runbooks" / "backup-restore.md").read_text()
        self.assertIn("env-secrets.md", backup_restore.split("## Off-cluster copies", 1)[1])
        self.assertIn("never", backup_restore.split("## Off-cluster copies", 1)[1], "the key apart from the data")
        self.assertEqual(text.count("same cluster"), 1, "the lab store's limit, stated once")

    def test_the_adr_is_listed_and_cites_its_sources(self):
        text = ADR.read_text()
        for needed in ("docs.percona.com", "velero.io/docs/main/restore-reference", "velero.io/docs/main/resource-filtering",
                       "docs.okd.io", "external-secrets.io", "SeaweedFS", "MinIO", "Accepted" if "Accepted" in text else "accepted"):
            self.assertIn(needed, text)
        index = (ROOT / "docs" / "adr" / "README.md").read_text()
        self.assertIn(f"[0020]({ADR.name})", index)


if __name__ == "__main__":
    unittest.main()
