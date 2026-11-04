# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The production Keycloak component (E9.F9.1.S3, issue #386): deploy/kustomize/components/keycloak-production, as
written and as rendered through the CI overlay. The Keycloak CR runs in production mode on the CloudNativePG Cluster
beside it, two instances behind TLS from a Secret, with a hostname, resources, and the database credentials read from
the Secret the CloudNativePG operator generates; the Cluster's image is pinned by digest; a NetworkPolicy admits 5432
to the database from Keycloak's pods only; no Secret is in Git; the kind job runs the proof at pinned operator
versions. The contracts quoted are the Keycloak operator's at 26.6.4 (operator/src/main/java/org/keycloak/operator)
and CloudNativePG's at v1.30.1 (pkg/specs/secrets.go), read at those tags; the on-cluster half is
scripts/dev/keycloak-production-e2e.sh."""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
KUSTOMIZE = ROOT / "deploy" / "kustomize"
COMPONENT = KUSTOMIZE / "components" / "keycloak-production"
OVERLAY = KUSTOMIZE / "overlays" / "ci-keycloak-production"
REALMS = KUSTOMIZE / "shared" / "keycloak-realms"
POSTGRES_IMAGE = re.compile(r"^ghcr\.io/cloudnative-pg/postgresql:[0-9][A-Za-z0-9._-]*@sha256:[0-9a-f]{64}$")
KEYCLOAK_CR = "keycloak"
CLUSTER = "keycloak-db"
APP_SECRET = f"{CLUSTER}-app"             # `<cluster>-app`, keys username and password (pkg/specs/secrets.go at v1.30.1)
# The operator's pod labels at 26.6.4: Constants.DEFAULT_LABELS plus INSTANCE_LABEL=<the CR's name> (Utils.allInstanceLabels);
# a realm import Job copies the template with app=keycloak-realm-import (KeycloakRealmImportJobDependentResource).
SERVER_PODS = {"app": "keycloak", "app.kubernetes.io/managed-by": "keycloak-operator", "app.kubernetes.io/instance": KEYCLOAK_CR}
IMPORT_PODS = {**SERVER_PODS, "app": "keycloak-realm-import"}
CNPG_OPERATOR_PODS = {"app.kubernetes.io/name": "cloudnative-pg"}


def load(path: pathlib.Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def one(docs: list[dict], kind: str, name: str) -> dict:
    (found,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return found


def quantity(text: str) -> int:
    """A Kubernetes memory quantity in bytes (the suffixes the components use)."""
    units = {"": 1, "M": 10**6, "G": 10**9, "Mi": 2**20, "Gi": 2**30}
    number, unit = re.fullmatch(r"(\d+)([A-Za-z]*)", text).groups()
    return int(number) * units[unit]


def render(path: pathlib.Path) -> list[dict]:
    """The objects `kubectl kustomize` renders (CI's manifests job renders every overlay the same way)."""
    out = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def check_component(case: unittest.TestCase, docs: list[dict], what: str) -> None:
    """What the component must render, from its files or through a kustomization: the story's acceptance criteria."""
    kc = one(docs, "Keycloak", KEYCLOAK_CR)
    case.assertEqual(kc["apiVersion"], "k8s.keycloak.org/v2beta1", f"{what}: Constants.CRDS_VERSION at 26.6.4; v2alpha1 is served deprecated")
    spec = kc["spec"]
    case.assertEqual(spec["instances"], 2, what)
    # Production mode is the operator's: `--verbose start`, and `--optimized` only for a custom image or startOptimized
    # (KeycloakDeploymentDependentResource.createBaseDeployment). No image and no startOptimized here, so `start`.
    case.assertNotIn("image", spec, f"{what}: the operator's default server image (RHBK's on OpenShift)")
    case.assertNotIn("startOptimized", spec, f"{what}: the default image is not pre-augmented")
    case.assertIs(spec["automountServiceAccountToken"], False, what)
    cluster = one(docs, "Cluster", CLUSTER)
    initdb = cluster["spec"]["bootstrap"]["initdb"]
    case.assertEqual(spec["db"], {
        "vendor": "postgres",
        "host": f"{CLUSTER}-rw", "port": 5432, "database": initdb["database"],
        "usernameSecret": {"name": APP_SECRET, "key": "username"},
        "passwordSecret": {"name": APP_SECRET, "key": "password"},
        "poolInitialSize": 10, "poolMinSize": 10, "poolMaxSize": 10,
    }, f"{what}: KeycloakDistConfigurator.configureDatabase at 26.6.4 on the Cluster's own Secret")
    case.assertEqual(spec["additionalOptions"], [{"name": "db-url-properties", "value": "?sslmode=require"}],
                     f"{what}: TLS to the database (DatabaseOptions.DB_URL_PROPERTIES at 26.6.4)")
    case.assertEqual(spec["http"], {"tlsSecret": "keycloak-tls"}, f"{what}: HTTPS from a Secret, HTTP off")
    case.assertRegex(spec["hostname"]["hostname"], r"^https://[a-z0-9.-]+(:\d+)?$", what)
    case.assertEqual(set(spec["resources"]), {"requests", "limits"}, what)
    case.assertEqual(set(spec["resources"]["requests"]), {"cpu", "memory"}, what)
    case.assertEqual(set(spec["resources"]["limits"]), {"memory"}, f"{what}: no CPU limit, as the other components")
    # The memory request covers a start: kc.sh commits half the limit as the initial heap (InitialRAMPercentage=50 at
    # 26.6.4) and a pod measured 1042 MiB at start on kind, so the request is at least the HA blueprint's 1250M
    # (deploy-keycloak.adoc:173 at 26.6.4) under a 2Gi limit.
    case.assertGreaterEqual(quantity(spec["resources"]["requests"]["memory"]), 1250 * 10**6, f"{what}: memory request")
    case.assertEqual(spec["resources"]["limits"]["memory"], "2Gi", what)
    # The Cluster: the pattern of components/openfga/cluster.yaml, the image pinned by digest.
    case.assertEqual(cluster["apiVersion"], "postgresql.cnpg.io/v1", what)
    case.assertRegex(cluster["spec"]["imageName"], POSTGRES_IMAGE, what)
    case.assertEqual(initdb, {"database": "keycloak", "owner": "keycloak"}, what)
    case.assertEqual(cluster["spec"]["instances"], 1, f"{what}: production patches the count (ADR-0014)")
    case.assertEqual(cluster["spec"]["managed"]["services"]["disabledDefaultServices"], ["ro", "r"],
                     f"{what}: Keycloak connects to -rw only (deploy-cnpg.adoc at 26.6.4)")
    case.assertEqual(cluster["spec"]["postgresql"]["pg_hba"],
                     ["hostssl keycloak keycloak all scram-sha-256", "host all keycloak all reject"], what)
    # No Secret anywhere: the credentials are the operator's, the certificate the platform's.
    case.assertFalse([d for d in docs if d["kind"] == "Secret"], f"{what}: no Secret in Git")
    # The database's NetworkPolicy: 5432 from Keycloak's pods (the server's and the import Jobs', both of this CR),
    # 8000 and 5432 from the CloudNativePG operator and the cluster's own pods, nothing else.
    policy = one(docs, "NetworkPolicy", CLUSTER)
    case.assertEqual(policy["spec"]["podSelector"], {"matchLabels": {"cnpg.io/cluster": CLUSTER}}, what)
    case.assertEqual(policy["spec"]["policyTypes"], ["Ingress"], what)
    keycloak_rule, operator_rule = policy["spec"]["ingress"]
    case.assertEqual(keycloak_rule["ports"], [{"port": 5432, "protocol": "TCP"}], what)
    case.assertEqual(keycloak_rule["from"], [{"podSelector": {"matchLabels": SERVER_PODS}},
                                             {"podSelector": {"matchLabels": IMPORT_PODS}}], what)
    for peer in keycloak_rule["from"]:
        case.assertNotIn("namespaceSelector", peer, f"{what}: this namespace only")
    case.assertEqual(sorted(p["port"] for p in operator_rule["ports"]), [5432, 8000], what)
    case.assertEqual(operator_rule["from"], [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "cnpg-system"}},
         "podSelector": {"matchLabels": CNPG_OPERATOR_PODS}},
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-operators"}},
         "podSelector": {"matchLabels": CNPG_OPERATOR_PODS}},
        {"podSelector": {"matchLabels": {"cnpg.io/cluster": CLUSTER}}},
    ], what)
    # A PodDisruptionBudget keeps one server pod through a node drain (two instances, so a drain or an upgrade may take
    # one). It selects what the operator's StatefulSet selects (Utils.allInstanceLabels at 26.6.4: the default labels
    # plus the instance label); the import Job's pods carry app=keycloak-realm-import and are not budgeted.
    pdb = one(docs, "PodDisruptionBudget", KEYCLOAK_CR)
    case.assertEqual(pdb["apiVersion"], "policy/v1", what)
    case.assertEqual(pdb["spec"], {"minAvailable": 1, "selector": {"matchLabels": SERVER_PODS}}, what)
    for obj in (kc, cluster, policy, pdb):
        case.assertEqual(obj["metadata"]["labels"]["app.kubernetes.io/part-of"], "inkcast", f"{what}: {obj['kind']}")


class ComponentFiles(unittest.TestCase):
    """The component's own files, without rendering (always runs)."""

    @classmethod
    def setUpClass(cls):
        k = yaml.safe_load((COMPONENT / "kustomization.yaml").read_text())
        cls.kustomization = k
        cls.docs = [d for name in k["resources"] for d in load(COMPONENT / name)]

    def test_the_component_renders_what_the_story_asks(self):
        self.assertEqual(self.kustomization["kind"], "Component")
        self.assertEqual(sorted(self.kustomization["resources"]), ["cluster.yaml", "keycloak.yaml", "networkpolicy.yaml", "pdb.yaml"])
        self.assertEqual(sorted(d["kind"] for d in self.docs), ["Cluster", "Keycloak", "NetworkPolicy", "PodDisruptionBudget"])
        check_component(self, self.docs, "component")
        kc = one(self.docs, "Keycloak", KEYCLOAK_CR)
        self.assertNotIn("unsupported", kc["spec"], "the Tech Preview pod template is a platform patch (the overlay's), not the component's")

    def test_the_hostname_in_the_component_cannot_serve(self):
        """Keycloak's hostname-strict defaults to true and requires `hostname` (HostnameV2Options at 26.6.4), and the
        value is the issuer of every realm, so no shared value is right: the component's is a name reserved as
        unresolvable (RFC 2606), and every kustomization that uses the component patches it (Rendered)."""
        kc = one(self.docs, "Keycloak", KEYCLOAK_CR)
        self.assertEqual(kc["spec"]["hostname"]["hostname"], "https://keycloak.example.invalid")

    def test_the_cr_name_is_the_one_the_lab_s_realm_imports_target(self):
        """scripts/gen-realm-imports.py writes spec.keycloakCRName for the lab's CR; the component's CR is that one, so
        shared/keycloak-realms imports into it unchanged (the kind proof applies exactly those files)."""
        for side in ("staging", "prod"):
            (realm_import,) = load(REALMS / f"realm-inkcast-{side}.yaml")
            self.assertEqual(realm_import["spec"]["keycloakCRName"], KEYCLOAK_CR, side)
            self.assertEqual(realm_import["metadata"]["namespace"], "keycloak", side)
        # The lab's Mailpit policy admits the server's pods by the same labels this component's policy relies on.
        (mailpit_policy,) = [d for d in load(REALMS / "networkpolicy.yaml") if d["kind"] == "NetworkPolicy"]
        (rule,) = mailpit_policy["spec"]["ingress"]
        self.assertEqual(rule["from"][0]["podSelector"]["matchLabels"], SERVER_PODS)

    def test_the_cluster_image_is_the_openfga_cluster_s_and_images_py_lists_it_pinned(self):
        """One CloudNativePG release, one PostgreSQL image (scripts/versions.env, CNPG_VERSION), seen by `images.py
        verify` through the Cluster's imageName like the OpenFGA one."""
        mine = one(self.docs, "Cluster", CLUSTER)["spec"]["imageName"]
        openfga = one(load(KUSTOMIZE / "components" / "openfga" / "cluster.yaml"), "Cluster", "openfga-db")["spec"]["imageName"]
        self.assertEqual(mine, openfga)
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "images.py"), "list"], capture_output=True, text=True,
                             check=True, cwd=ROOT).stdout
        listed = [line for line in out.splitlines() if "components/keycloak-production/" in line]
        self.assertEqual(len(listed), 1, out)
        self.assertIn("cluster.yaml", listed[0])
        self.assertNotIn("not pinned", listed[0])

    def test_the_ci_overlay_patches_only_what_a_platform_supplies(self):
        """The hostname and, for Pod Security "restricted" on plain Kubernetes, the securityContext the operator does
        not set (none in operator/src/main at 26.6.4) through spec.unsupported.podTemplate; nothing else."""
        k = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
        self.assertEqual(k["namespace"], "keycloak", "the lab's namespace, gen-realm-imports.py's NAMESPACE")
        self.assertEqual(k["components"], ["../../components/keycloak-production"])
        (patch,) = load(OVERLAY / k["patches"][0]["path"])
        self.assertEqual((patch["kind"], patch["metadata"]["name"]), ("Keycloak", KEYCLOAK_CR))
        self.assertEqual(set(patch["spec"]), {"hostname", "unsupported"})
        pod = patch["spec"]["unsupported"]["podTemplate"]["spec"]
        self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}})
        (container,) = pod["containers"]     # unnamed: the operator names its first container and warns on any name given
        self.assertEqual(container, {"securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}})
        namespace = one(load(OVERLAY / "namespace.yaml"), "Namespace", "keycloak")
        dev = one(load(ROOT / "deploy" / "keycloak" / "keycloak.yaml"), "Namespace", "keycloak")
        self.assertEqual(namespace["metadata"]["labels"], dev["metadata"]["labels"], "one object, applied by both")


def kustomizations_using(component: pathlib.Path) -> dict[str, pathlib.Path]:
    """Every kustomization under deploy/kustomize whose `components` list names the component, by its path relative
    to deploy/kustomize: a new overlay, environment or shared kustomization that uses it is tested without being
    listed here (review of #391, K6)."""
    found = {}
    for path in sorted(KUSTOMIZE.rglob("kustomization.yaml")):
        k = yaml.safe_load(path.read_text()) or {}
        if k.get("kind") == "Component":
            continue
        for entry in k.get("components") or []:
            if (path.parent / entry).resolve() == component.resolve():
                found[str(path.parent.relative_to(KUSTOMIZE))] = path.parent
    return found


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the overlays (CI's manifests job has it)")
class Rendered(unittest.TestCase):
    """The rendered objects of every kustomization that ships the component, found by scanning deploy/kustomize."""

    @classmethod
    def setUpClass(cls):
        cls.docs = {name: render(path) for name, path in kustomizations_using(COMPONENT).items()}

    def test_the_scan_finds_the_ci_overlay(self):
        self.assertIn("overlays/ci-keycloak-production", self.docs)

    def test_every_kustomization_renders_the_component_in_its_namespace_with_its_own_hostname(self):
        for name, docs in self.docs.items():
            with self.subTest(kustomization=name):
                check_component(self, [d for d in docs if d["kind"] != "Namespace"], name)
                kc = one(docs, "Keycloak", KEYCLOAK_CR)
                self.assertNotEqual(kc["spec"]["hostname"]["hostname"], "https://keycloak.example.invalid",
                                    f"{name}: the component's unresolvable hostname was not patched")
                self.assertNotIn("example.invalid", kc["spec"]["hostname"]["hostname"], f"{name}: the hostname is patched")
                for d in docs:
                    if d["kind"] != "Namespace":
                        self.assertEqual(d["metadata"]["namespace"], "keycloak", f"{name}: {d['kind']} {d['metadata']['name']}")
                namespace = one(docs, "Namespace", "keycloak")
                self.assertEqual(namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted", name)


class KindJob(unittest.TestCase):
    """The kind CI job runs scripts/dev/keycloak-production-e2e.sh, with the Keycloak operator pinned like CloudNativePG
    (scripts/versions.env: the release and a SHA-256 per install file, checked before applying)."""

    def test_the_operator_is_pinned_and_the_kind_job_runs_the_script(self):
        versions = dict(re.findall(r"^([A-Za-z0-9_]+)=(.*)$", (ROOT / "scripts" / "versions.env").read_text(), re.M))
        self.assertRegex(versions["KEYCLOAK_OPERATOR_VERSION"], r"^26\.\d+\.\d+$")
        for key in ("KEYCLOAK_OPERATOR_CRD_KEYCLOAKS_SHA256", "KEYCLOAK_OPERATOR_CRD_REALMIMPORTS_SHA256",
                    "KEYCLOAK_OPERATOR_MANIFEST_SHA256"):
            self.assertRegex(versions[key], r"^[0-9a-f]{64}$", key)
        script = (ROOT / "scripts" / "dev" / "keycloak-production-e2e.sh").read_text()
        for needed in ("KEYCLOAK_OPERATOR_VERSION", "KEYCLOAK_OPERATOR_CRD_KEYCLOAKS_SHA256",
                       "KEYCLOAK_OPERATOR_CRD_REALMIMPORTS_SHA256", "KEYCLOAK_OPERATOR_MANIFEST_SHA256",
                       "CNPG_MANIFEST_SHA256", "apply --server-side", "overlays/ci-keycloak-production",
                       "shared/keycloak-realms", "condition=Ready keycloak/keycloak", "condition=Done",
                       "pg_stat_ssl", "openid-configuration", "delete pod keycloak-0"):
            self.assertIn(needed, script)
        self.assertNotIn("sha256sum -c", script, "not on macOS: compare the digest as scripts/kind-tools.sh does")
        self.assertNotIn("| grep -q", script, "under pipefail a writer can die of SIGPIPE: parse, don't grep")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("scripts/dev/keycloak-production-e2e.sh", ci)
        self.assertLess(ci.index("scripts/dev/openfga-e2e.sh"), ci.index("scripts/dev/keycloak-production-e2e.sh"),
                        "after the OpenFGA step, which installs CloudNativePG")
        self.assertIn("keycloak", ci.split("name: Diagnostics")[1])


if __name__ == "__main__":
    unittest.main()
