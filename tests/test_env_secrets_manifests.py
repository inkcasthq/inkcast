# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""components/env-secrets as every target renders it (E3.F3.9.S7, issue #323): the init container in OpenFGA's pod
of each lab environment (the release's own image, restricted, the API token projected into it alone), its Roles by
resource name in the environment's namespace, in inkcast-db and in keycloak, the names agreeing with the code
(src/inkcast/env_secrets.py) and with every consumer, the self-host overlays carrying none of it, the CI overlays
mirroring the environment, and no secret step left. scripts/dev/env-secrets-e2e.sh proves the same on kind."""

from __future__ import annotations

import copy
import pathlib
import re
import shutil
import unittest

import yaml
from test_openfga_component import KUSTOMIZE, one, pod_spec, render

from inkcast.env_secrets import DB_HOST, DB_NAMESPACE, KEYCLOAK_NAMESPACE, SERVICE_ACCOUNT, Layout

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENVIRONMENTS = {"environments/staging": "inkcast-staging", "environments/production": "inkcast-prod"}
OVERLAYS = ("overlays/kubernetes", "overlays/openshift", "overlays/ci-openfga")
SA = "inkcast-env-secrets"
PULL = [{"name": "ghcr-pull"}]
RESTRICTED = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
INKCAST_IMAGE = re.compile(r"^ghcr\.io/inkcasthq/inkcast@sha256:[0-9a-f]{64}$")
TOKEN_VOLUME = {"name": "api-token", "projected": {"sources": [
    {"serviceAccountToken": {"path": "token", "expirationSeconds": 600}},
    {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
    {"downwardAPI": {"items": [{"path": "namespace", "fieldRef": {"fieldPath": "metadata.namespace"}}]}},
]}}
TOKEN_MOUNT = [{"name": "api-token", "mountPath": str(SERVICE_ACCOUNT), "readOnly": True}]


def secrets_rule(names: list[str] | None, verbs: list[str]) -> dict:
    rule = {"apiGroups": [""], "resources": ["secrets"], "verbs": verbs}
    if names is not None:
        rule["resourceNames"] = names
    return rule


def of_kind(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def env_by_name(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {name: render(name) for name in (*ENVIRONMENTS, *OVERLAYS, "shared/inkcast-db",
                                                     "shared/keycloak-access", "overlays/ci-env-secrets",
                                                     "overlays/ci-inkcast-db")}

    def test_the_init_container_runs_the_release_s_image_restricted_with_the_token_in_it_alone(self):
        for name, namespace in ENVIRONMENTS.items():
            with self.subTest(environment=name):
                docs = self.docs[name]
                pod = pod_spec(one(docs, "Deployment", "openfga"))
                init, migrate = pod["initContainers"]   # env-secrets first: migrate reads the Secret it makes (#337)
                self.assertEqual((init["name"], init["args"]), ("env-secrets", ["env-secrets"]))
                self.assertEqual(migrate["name"], "migrate")
                self.assertNotIn("command", init, "the image's entrypoint, the launcher")
                inkcast_image = pod_spec(one(docs, "Deployment", "inkcast"))["containers"][0]["image"]
                self.assertEqual(init["image"], inkcast_image, "the environment's digest: every release runs it")
                self.assertRegex(init["image"], INKCAST_IMAGE)
                self.assertEqual(init["securityContext"], RESTRICTED)
                self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                                 "no uid or fsGroup: restricted-v2 assigns them")
                self.assertFalse({"env", "envFrom"} & set(init), "it reads no value from its environment")
                self.assertEqual(init["volumeMounts"], TOKEN_MOUNT)
                self.assertIn("memory", init["resources"]["limits"])
                self.assertIs(pod["automountServiceAccountToken"], False, "no token for OpenFGA's own container")
                self.assertEqual(pod["volumes"], [TOKEN_VOLUME], "ten minutes, the shortest the API allows")
                for container in (*pod["containers"], migrate):
                    self.assertFalse(container.get("volumeMounts"), f"{container['name']}: never the token")
                self.assertEqual((pod["serviceAccountName"], pod["imagePullSecrets"]), (SA, PULL))
                account = one(docs, "ServiceAccount", SA)
                self.assertEqual(account["metadata"]["namespace"], namespace)
                self.assertIs(account["automountServiceAccountToken"], False)
                self.assertEqual(account["imagePullSecrets"], PULL)
                self.assertFalse([d for d in docs if d["kind"] == "Secret"], "nothing generated by Kustomize")

    def test_the_environment_s_role_names_its_two_secrets_and_creates(self):
        for name, namespace in ENVIRONMENTS.items():
            with self.subTest(environment=name):
                docs = self.docs[name]
                self.assertEqual(set(of_kind(docs, "Role")), {SA})
                self.assertFalse([d for d in docs if d["kind"] in ("ClusterRole", "ClusterRoleBinding")])
                role, binding = one(docs, "Role", SA), one(docs, "RoleBinding", SA)
                self.assertEqual(role["metadata"]["namespace"], namespace)
                self.assertEqual(role["rules"], [secrets_rule(["inkcast-secrets"], ["get", "patch"]),
                                                 secrets_rule(["openfga-db-app"], ["get"]),
                                                 secrets_rule(None, ["create"])])
                self.assertEqual(binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": SA})
                self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": SA, "namespace": namespace}])

    def test_the_shared_namespaces_grant_each_side_its_own_secret_only(self):
        verbs = {"shared/inkcast-db": ["get"], "shared/keycloak-access": ["get", "patch"]}
        for shared, namespace in (("shared/inkcast-db", DB_NAMESPACE), ("shared/keycloak-access", KEYCLOAK_NAMESPACE)):
            docs = self.docs[shared]
            roles, bindings = of_kind(docs, "Role"), of_kind(docs, "RoleBinding")
            self.assertEqual(set(roles), {f"{SA}-staging", f"{SA}-prod"}, shared)
            self.assertEqual(set(bindings), set(roles), shared)
            for app_namespace in ENVIRONMENTS.values():
                layout = Layout.for_namespace(app_namespace)
                with self.subTest(shared=shared, side=layout.side):
                    own = layout.db_role if namespace == DB_NAMESPACE else layout.realm
                    self.assertEqual(own.namespace, namespace)
                    role, binding = roles[f"{SA}-{layout.side}"], bindings[f"{SA}-{layout.side}"]
                    self.assertEqual(role["metadata"]["namespace"], namespace)
                    self.assertEqual(role["rules"], [secrets_rule([own.name], verbs[shared]), secrets_rule(None, ["create"])])
                    self.assertEqual(binding["metadata"]["namespace"], namespace)
                    self.assertEqual(binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                                                          "name": role["metadata"]["name"]})
                    self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": SA, "namespace": app_namespace}])
        keycloak = self.docs["shared/keycloak-access"]
        self.assertEqual({d["kind"] for d in keycloak}, {"Role", "RoleBinding"}, "Keycloak's installation owns the namespace")

    def test_the_names_are_the_code_s_and_every_consumer_s(self):
        cluster = one(self.docs["shared/inkcast-db"], "Cluster", "inkcast-pg")
        roles = {r["name"]: r for r in cluster["spec"]["managed"]["roles"]}
        self.assertEqual(DB_HOST, f"{cluster['metadata']['name']}-rw.{cluster['metadata']['namespace']}.svc",
                         "the operator's read-write Service")
        for name, namespace in ENVIRONMENTS.items():
            layout = Layout.for_namespace(namespace)
            with self.subTest(environment=name):
                docs = self.docs[name]
                self.assertEqual(roles[layout.role]["passwordSecret"], {"name": layout.db_role.name})
                self.assertEqual(layout.db_role.namespace, cluster["metadata"]["namespace"])
                self.assertEqual((layout.app.namespace, layout.db_app.namespace), (namespace, namespace))
                openfga = pod_spec(one(docs, "Deployment", "openfga"))
                (migrate,) = [c for c in openfga["initContainers"] if c["name"] == "migrate"]   # the schema, before the server (#337)
                for container in (openfga["containers"][0], migrate):
                    env = env_by_name(container)
                    self.assertEqual(env["OPENFGA_DATASTORE_URI"]["valueFrom"],
                                     {"secretKeyRef": {"name": layout.db_app.name, "key": "uri"}}, container["name"])
                env = env_by_name(pod_spec(one(docs, "Deployment", "openfga"))["containers"][0])
                self.assertEqual(env["OPENFGA_AUTHN_PRESHARED_KEYS"]["valueFrom"],
                                 {"secretKeyRef": {"name": layout.app.name, "key": "fga-api-token"}})
                inkcast = pod_spec(one(docs, "Deployment", "inkcast"))
                (volume,) = [v for v in inkcast["volumes"] if "secret" in v]
                self.assertEqual(volume["secret"]["secretName"], layout.app.name)
                (mount,) = [m for m in inkcast["containers"][0]["volumeMounts"] if m["name"] == volume["name"]]
                config = one(docs, "ConfigMap", "inkcast-config")["data"]
                files = [v for v in (*config.values(), *(e.get("value", "") for e in inkcast["containers"][0]["env"]))
                         if v.startswith(mount["mountPath"] + "/")]
                read = {pathlib.PurePosixPath(f).name for f in files}
                self.assertLessEqual({"secret-key", "oidc-client-secret", "fga-api-token"}, read)
                written = {"secret-key", "fga-api-token", "oidc-client-secret", "keycloak-sync-secret"}
                self.assertLessEqual(read, written, "every key Inkcast reads from the Secret is one env-secrets writes")

    def test_the_self_host_overlays_carry_none_of_it(self):
        for name in OVERLAYS:
            with self.subTest(overlay=name):
                docs = self.docs[name]
                pod = pod_spec(one(docs, "Deployment", "openfga"))
                self.assertEqual([c["name"] for c in pod["initContainers"]], ["migrate"], "the component's own init container only")
                self.assertNotIn("serviceAccountName", pod)
                self.assertFalse([d for d in docs if d["kind"] in ("Role", "RoleBinding")])
                self.assertNotIn("env-secrets", yaml.safe_dump_all(docs))

    def test_the_ci_overlays_are_the_environment_s_but_for_the_image_and_the_operator_s_namespace(self):
        staging, ci = self.docs["environments/staging"], self.docs["overlays/ci-env-secrets"]
        expected = copy.deepcopy(pod_spec(one(staging, "Deployment", "openfga")))
        expected["initContainers"][0].update(image="inkcast:ci", imagePullPolicy="Never")
        self.assertEqual(pod_spec(one(ci, "Deployment", "openfga")), expected)
        for kind, name in (("Role", SA), ("RoleBinding", SA), ("ServiceAccount", SA)):
            self.assertEqual(one(ci, kind, name), one(staging, kind, name), kind)
        self.assertFalse([d for d in ci if d["kind"] in ("Secret", "Cluster")], "no Secret, and the shared database")
        self.assertEqual(one(ci, "ConfigMap", "inkcast-config")["data"]["INKCAST_AUTHZ_ENGINE"], "local")
        shared, kind_db = self.docs["shared/inkcast-db"], self.docs["overlays/ci-inkcast-db"]
        for theirs in shared:
            ours = one(kind_db, theirs["kind"], theirs["metadata"]["name"])
            if theirs["kind"] != "NetworkPolicy":
                self.assertEqual(ours, theirs, theirs["kind"])
        operator = one(kind_db, "NetworkPolicy", "inkcast-pg")["spec"]["ingress"][1]["from"][0]["namespaceSelector"]
        self.assertEqual(operator, {"matchLabels": {"kubernetes.io/metadata.name": "cnpg-system"}})
        self.assertEqual(len(kind_db), len(shared))


class NoSecretStep(unittest.TestCase):
    """The bring-up has no secret step left, and the kind job proves the cluster makes them."""

    def test_the_credentials_script_is_gone_and_the_readme_names_the_keycloak_access(self):
        self.assertFalse((ROOT / "scripts" / "deploy" / "openfga-db-credentials.sh").exists())
        readme = (ROOT / "README.md").read_text()
        self.assertNotIn("openfga-db-credentials", readme)
        self.assertIn("oc apply -k deploy/kustomize/shared/keycloak-access", readme)
        lab = readme.split("deploy/kustomize/shared/keycloak-access", 1)[1].split("```", 1)[0]
        self.assertNotIn("create secret", lab, "the lab's block makes no Secret but the pull Secret's script")
        for path in ("environments/staging/kustomization.yaml", "environments/production/kustomization.yaml"):
            self.assertIn("- ../../components/env-secrets", (KUSTOMIZE / path).read_text(), path)

    def test_the_kind_job_runs_the_script_after_the_openfga_one(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("scripts/dev/env-secrets-e2e.sh", ci)
        self.assertLess(ci.index("scripts/dev/openfga-e2e.sh"), ci.index("scripts/dev/env-secrets-e2e.sh"),
                        "the OpenFGA script installs the CloudNativePG operator")
        diagnostics = ci.split("name: Diagnostics")[1]
        for namespace in ("inkcast-staging", "inkcast-db"):
            self.assertIn(namespace, diagnostics)
        script = (ROOT / "scripts" / "dev" / "env-secrets-e2e.sh").read_text()
        for needed in ("overlays/ci-inkcast-db", "shared/keycloak-access", "overlays/ci-env-secrets", "check absent",
                       "check no-value", "rollout restart deploy/openfga", "disagree"):
            self.assertIn(needed, script)


if __name__ == "__main__":
    unittest.main()
