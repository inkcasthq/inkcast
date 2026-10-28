# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The lab's Keycloak realms as code (issue #305, D1 and D2): one ``KeycloakRealmImport`` per environment, generated
by scripts/gen-realm-imports.py from the one realm file (deploy/keycloak/realm-inkcast.json) and the environment's own
settings, so the realm's name, the client's redirect and the environment's issuer agree by construction; the client
secrets only ever as placeholders fed from the realm Secret the secrets Job writes (#323); the realms' mail caught by
a Mailpit that only Keycloak may reach; and the user sync on in both environments, reading the same Secret.

The operator's contract is the one at Keycloak 26.6.7 (operator/src/main/java/org/keycloak/operator at that tag):
``k8s.keycloak.org/v2beta1`` (Constants.CRDS_VERSION; v2alpha1 is served deprecated), ``spec.keycloakCRName``,
``spec.realm`` (a RealmRepresentation) and ``spec.placeholders`` as ``{ENV: {secret: {name, key}}}``, each becoming an
environment variable of the import Job from that Secret key (KeycloakRealmImportJobDependentResource.desired), which
``kc.sh import`` resolves in the realm text (AbstractFileBasedImportProvider; an unresolved ``${X}`` stays literal,
StringPropertyReplacer). The on-cluster half (both imports Done, a sign-in in each realm, the verification mail in
Mailpit) is measured by the integrator on OpenShift Local."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "gen-realm-imports.py"
KUSTOMIZE = ROOT / "deploy" / "kustomize"
SHARED = KUSTOMIZE / "shared" / "keycloak-realms"
REALM_FILE = ROOT / "deploy" / "keycloak" / "realm-inkcast.json"
DEV_KEYCLOAK = ROOT / "deploy" / "keycloak" / "keycloak.yaml"
ENVIRONMENTS = {"staging": "staging", "production": "prod"}      # directory: the suffix of every name
NAMESPACE = "keycloak"                                           # the lab's Keycloak, its CR and its Secrets
KEYCLOAK_CR = "keycloak"
SECRETS_DIR = "/var/run/secrets/inkcast"                         # where the base mounts Secret inkcast-secrets
# The realm file's secret placeholders, and the key of Secret inkcast-<side>-realm that feeds each (#323's contract).
PLACEHOLDERS = {"KC_INKCAST_CLIENT_SECRET": "identity-client-secret", "KC_INKCAST_SYNC_SECRET": "sync-client-secret"}
# The import Job's pods carry app=keycloak-realm-import instead of app=keycloak (KeycloakRealmImportJobDependentResource),
# so this selects the server's pods only: Constants.DEFAULT_LABELS plus INSTANCE_LABEL=<the Keycloak CR's name>.
KEYCLOAK_PODS = {"app": "keycloak", "app.kubernetes.io/managed-by": "keycloak-operator", "app.kubernetes.io/instance": KEYCLOAK_CR}
MAILPIT_IMAGE = re.compile(r"^axllent/mailpit:v1\.28\.3@sha256:[0-9a-f]{64}$")
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def load(path: pathlib.Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def one(docs: list[dict], kind: str, name: str) -> dict:
    (found,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return found


def literals(env: str) -> dict[str, str]:
    """The environment's inkcast-config literals, read from its kustomization (no rendering)."""
    k = yaml.safe_load((KUSTOMIZE / "environments" / env / "kustomization.yaml").read_text())
    generator = next(g for g in k["configMapGenerator"] if g["name"] == "inkcast-config")
    return dict(lit.split("=", 1) for lit in generator["literals"])


def generator_module():
    spec = importlib.util.spec_from_file_location("gen_realm_imports", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_inputs(root: pathlib.Path) -> None:
    """The generator's inputs and outputs, at the same relative paths under another root."""
    for relative in ("deploy/keycloak/realm-inkcast.json", "deploy/kustomize/environments/staging/kustomization.yaml",
                     "deploy/kustomize/environments/production/kustomization.yaml"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, root / relative)
    shutil.copytree(SHARED, root / SHARED.relative_to(ROOT))


class GeneratedImports(unittest.TestCase):
    """The generated files, without rendering (always runs)."""

    @classmethod
    def setUpClass(cls):
        cls.realm_text = REALM_FILE.read_text()
        cls.realm = json.loads(cls.realm_text)
        cls.imports = {}
        for env, side in ENVIRONMENTS.items():
            path = SHARED / f"realm-inkcast-{side}.yaml"
            (doc,) = load(path)
            cls.imports[env] = (path, path.read_text(), doc)

    def test_one_import_per_environment_in_the_keycloak_namespace(self):
        kustomization = yaml.safe_load((SHARED / "kustomization.yaml").read_text())
        self.assertEqual(kustomization["namespace"], NAMESPACE)
        for env, side in ENVIRONMENTS.items():
            with self.subTest(env=env):
                path, text, doc = self.imports[env]
                self.assertIn(path.name, kustomization["resources"])
                self.assertTrue(text.startswith("# GENERATED by scripts/gen-realm-imports.py"), "a header says not to edit")
                self.assertEqual(doc["apiVersion"], "k8s.keycloak.org/v2beta1")
                self.assertEqual(doc["kind"], "KeycloakRealmImport")
                self.assertEqual(doc["metadata"]["name"], f"inkcast-{side}")
                self.assertEqual(doc["metadata"]["namespace"], NAMESPACE)
                self.assertEqual(doc["spec"]["keycloakCRName"], KEYCLOAK_CR)
                self.assertEqual(doc["spec"]["realm"]["realm"], f"inkcast-{side}")
                self.assertEqual(set(doc["spec"]), {"keycloakCRName", "placeholders", "realm"}, "the v2beta1 fields only")

    def test_the_realm_is_the_realm_file_with_only_its_name_urls_and_mail_substituted(self):
        """D1: the realm file unchanged. Everything but the realm's name, the public URL and the SMTP block is equal, so
        open sign-up, verification, the terms, the token lifetimes and the roles are the file's (tests/test_keycloak_realm.py)."""
        for env, side in ENVIRONMENTS.items():
            with self.subTest(env=env):
                public_url = literals(env)["INKCAST_PUBLIC_URL"]
                expected = json.loads(self.realm_text.replace("${INKCAST_PUBLIC_URL}", public_url))
                expected["realm"] = f"inkcast-{side}"
                del expected["smtpServer"]
                got = dict(self.imports[env][2]["spec"]["realm"])
                del got["smtpServer"]
                self.assertEqual(got, expected)

    def test_redirects_and_issuer_agree_with_the_environment(self):
        """The values that must agree per side: the client's redirect is the environment's public URL + /auth/callback
        (the Route host, #314), the environment's OIDC_ISSUER names this realm on the lab's Keycloak, its OIDC_CLIENT_ID
        is a client of this realm, and its sync client is the realm's service account."""
        for env, side in ENVIRONMENTS.items():
            with self.subTest(env=env):
                settings = literals(env)
                public_url = settings["INKCAST_PUBLIC_URL"]
                realm = self.imports[env][2]["spec"]["realm"]
                clients = {c["clientId"]: c for c in realm["clients"]}
                identity = clients[settings["OIDC_CLIENT_ID"]]
                self.assertEqual(identity["redirectUris"], [f"{public_url}/auth/callback"])
                self.assertEqual(identity["webOrigins"], [public_url])
                self.assertEqual(identity["baseUrl"], f"{public_url}/account/return")
                self.assertEqual(identity["attributes"]["post.logout.redirect.uris"], f"{public_url}/")
                self.assertEqual(settings["OIDC_ISSUER"], f"https://keycloak.apps-crc.testing/realms/{realm['realm']}")
                self.assertNotIn("${", json.dumps({k: v for k, v in identity.items() if k != "secret"}),
                                 "no placeholder left in the client but its secret")
                sync = clients[settings["INKCAST_KEYCLOAK_SYNC_CLIENT_ID"]]
                self.assertTrue(sync["serviceAccountsEnabled"], "the sync signs in as a service account (D2)")

    def test_secrets_are_placeholders_fed_from_the_realm_secret_and_nothing_else(self):
        """No secret value is rendered: each client's secret stays ``${...}``, every placeholder left in the text is
        declared under spec.placeholders (an undeclared one would reach Keycloak literally), and each is fed from the
        key of Secret inkcast-<side>-realm that the secrets Job writes (#323)."""
        for env, side in ENVIRONMENTS.items():
            with self.subTest(env=env):
                _, text, doc = self.imports[env]
                placeholders = doc["spec"]["placeholders"]
                self.assertEqual(set(PLACEHOLDER.findall(text)), set(placeholders))
                self.assertEqual(set(placeholders), set(PLACEHOLDERS))
                for name, key in PLACEHOLDERS.items():
                    self.assertEqual(placeholders[name], {"secret": {"name": f"inkcast-{side}-realm", "key": key}}, name)
                realm = doc["spec"]["realm"]
                for client in realm["clients"]:
                    self.assertRegex(client["secret"], r"^\$\{[A-Z0-9_]+\}$", client["clientId"])
                for user in realm.get("users", []):
                    self.assertIn("serviceAccountClientId", user, "no people in Git")
                    self.assertNotIn("credentials", user)
                self.assertNotIn("password", realm["smtpServer"], "Mailpit takes no password; none is rendered")
                self.assertNotIn("user", realm["smtpServer"])

    def test_mail_goes_to_mailpit_in_the_keycloak_namespace(self):
        """D1: the lab's mail (verification, password reset) lands in Mailpit. The realm's SMTP host and port are the
        Service's, by its name in this namespace, plain SMTP without auth; the From names the environment."""
        service = one(load(SHARED / "mailpit.yaml"), "Service", "mailpit")
        smtp_port = next(p for p in service["spec"]["ports"] if p["name"] == "smtp")["port"]
        for env, side in ENVIRONMENTS.items():
            with self.subTest(env=env):
                smtp = self.imports[env][2]["spec"]["realm"]["smtpServer"]
                host = literals(env)["INKCAST_PUBLIC_URL"].removeprefix("https://")
                self.assertEqual(smtp, {"host": f"mailpit.{NAMESPACE}.svc", "port": str(smtp_port), "from": f"no-reply@{host}",
                                        "fromDisplayName": self.realm["smtpServer"]["fromDisplayName"],
                                        "auth": "false", "starttls": "false", "ssl": "false"})

    def test_check_passes_on_the_tree_and_catches_drift(self):
        """``--check`` is the gate: current files pass; a hand edit of a generated file, a change to the realm file, to an
        environment's public URL or to the Mailpit Service each fail it until the generator runs again."""
        result = subprocess.run([sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        gen = generator_module()
        staging = SHARED.relative_to(ROOT) / "realm-inkcast-staging.yaml"
        drifts = {
            "a generated file edited": (staging, lambda t: t.replace("/auth/callback", "/auth/elsewhere")),
            "the realm file changed": (pathlib.Path("deploy/keycloak/realm-inkcast.json"),
                                       lambda t: t.replace('"verifyEmail": true', '"verifyEmail": false')),
            "the public URL moved": (pathlib.Path("deploy/kustomize/environments/staging/kustomization.yaml"),
                                     lambda t: t.replace("INKCAST_PUBLIC_URL=https://inkcast-staging.apps-crc.testing",
                                                         "INKCAST_PUBLIC_URL=https://staging.example.com")),
            "the SMTP port moved": (SHARED.relative_to(ROOT) / "mailpit.yaml", lambda t: t.replace("port: 1025", "port: 2525")),
        }
        for what, (relative, change) in drifts.items():
            with self.subTest(drift=what), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                copy_inputs(root)
                self.assertEqual(gen.main(["--check"], root=root), 0, "the copy starts current")
                path = root / relative
                before = path.read_text()
                path.write_text(change(before))
                self.assertNotEqual(path.read_text(), before, "the drift was applied")
                self.assertEqual(gen.main(["--check"], root=root), 1)
                self.assertEqual(gen.main([], root=root), 0, "regenerating")
                self.assertEqual(gen.main(["--check"], root=root), 0, "then current again")

    def test_the_generator_refuses_what_it_cannot_make_agree(self):
        """A placeholder the contract does not feed would stay literal in Keycloak; an issuer naming another realm would
        send people to a realm that does not exist. Both stop the generator, naming the culprit."""
        gen = generator_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            copy_inputs(root)
            realm = root / "deploy" / "keycloak" / "realm-inkcast.json"
            realm.write_text(realm.read_text().replace('"rememberMe": false', '"rememberMe": "${KC_REMEMBER_ME}"'))
            with self.assertRaises(SystemExit) as refused:
                gen.files(root)
            self.assertIn("KC_REMEMBER_ME", str(refused.exception))
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            copy_inputs(root)
            env = root / "deploy" / "kustomize" / "environments" / "production" / "kustomization.yaml"
            env.write_text(env.read_text().replace("/realms/inkcast-prod", "/realms/inkcast"))
            with self.assertRaises(SystemExit) as refused:
                gen.files(root)
            self.assertIn("OIDC_ISSUER", str(refused.exception))
            self.assertIn("production", str(refused.exception))

    def test_the_realm_file_and_its_ci_consumers_are_untouched(self):
        """The ci-oidc overlays and the kind job import the realm file as it is: its placeholders and the dev Keycloak
        that fills them are the ones tests/test_keycloak_realm.py guards, and the generator reads, never writes, it."""
        self.assertEqual(set(PLACEHOLDER.findall(self.realm_text)) & set(PLACEHOLDERS), set(PLACEHOLDERS))
        self.assertEqual(self.realm["realm"], "inkcast")
        source = GENERATOR.read_text()
        self.assertNotIn("realm-inkcast.json\").write", source)
        kustomization = yaml.safe_load((ROOT / "deploy" / "keycloak" / "kustomization.yaml").read_text())
        self.assertEqual(kustomization["configMapGenerator"][0]["files"], ["realm-inkcast.json"])


class Mailpit(unittest.TestCase):
    """The lab's mail catcher in the Keycloak namespace: restricted-v2, pinned, and reachable on SMTP by Keycloak only."""

    @classmethod
    def setUpClass(cls):
        cls.docs = load(SHARED / "mailpit.yaml")
        cls.deployment = one(cls.docs, "Deployment", "mailpit")
        cls.service = one(cls.docs, "Service", "mailpit")
        (cls.policy,) = load(SHARED / "networkpolicy.yaml")

    def test_the_pod_fits_restricted_v2_with_the_image_the_dev_keycloak_pins(self):
        pod = self.deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                         "no runAsUser or fsGroup: the restricted-v2 SCC assigns them (as components/openfga)")
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertIs(pod["enableServiceLinks"], False)
        (container,) = pod["containers"]
        self.assertEqual(container["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                        "capabilities": {"drop": ["ALL"]}})
        self.assertRegex(container["image"], MAILPIT_IMAGE)
        dev = next(d for d in load(DEV_KEYCLOAK) if d["kind"] == "Deployment" and d["metadata"]["name"] == "mailpit")
        self.assertEqual(container["image"], dev["spec"]["template"]["spec"]["containers"][0]["image"], "one pin for both")
        self.assertEqual({p["name"]: p["containerPort"] for p in container["ports"]}, {"smtp": 1025, "http": 8025})
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/readyz", "port": "http"})
        self.assertEqual([m["mountPath"] for m in container["volumeMounts"]], ["/tmp"], "its message store, nothing else")
        self.assertEqual(pod["volumes"], [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}])
        self.assertIn("memory", container["resources"]["limits"])

    def test_the_service_fronts_the_pod_on_both_ports(self):
        labels = self.deployment["spec"]["template"]["metadata"]["labels"]
        self.assertLessEqual(self.service["spec"]["selector"].items(), labels.items())
        self.assertLessEqual(self.deployment["spec"]["selector"]["matchLabels"].items(), labels.items())
        self.assertEqual({p["name"]: (p["port"], p["targetPort"]) for p in self.service["spec"]["ports"]},
                         {"smtp": (1025, "smtp"), "http": (8025, "http")})
        for doc in self.docs + [self.policy]:
            self.assertNotIn("namespace", doc["metadata"], "the kustomization sets the namespace, once")

    def test_only_keycloak_s_pods_reach_smtp(self):
        """Ingress to 1025 from the Keycloak server's pods in this namespace (Constants.DEFAULT_LABELS plus the
        instance label at 26.6.7; the import Job's pods carry another app label and send no mail) and nothing else:
        the API port is open to no pod (read the mail with oc port-forward). The readiness probe comes from the node,
        outside NetworkPolicy on OVN-Kubernetes, as the shared database's policy already relies on."""
        labels = self.deployment["spec"]["template"]["metadata"]["labels"]
        self.assertLessEqual(self.policy["spec"]["podSelector"]["matchLabels"].items(), labels.items())
        self.assertIn("app.kubernetes.io/name", self.policy["spec"]["podSelector"]["matchLabels"])
        self.assertEqual(self.policy["spec"]["policyTypes"], ["Ingress"])
        (rule,) = self.policy["spec"]["ingress"]
        (peer,) = rule["from"]
        self.assertEqual(peer, {"podSelector": {"matchLabels": KEYCLOAK_PODS}}, "same namespace, these pods only")
        self.assertEqual(rule["ports"], [{"port": 1025, "protocol": "TCP"}])


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
class Rendered(unittest.TestCase):
    def render(self, path: str) -> list[dict]:
        out = subprocess.run(["kubectl", "kustomize", str(KUSTOMIZE / path)], capture_output=True, text=True, check=True)
        return [d for d in yaml.safe_load_all(out.stdout) if d]

    def test_the_shared_kustomization_renders_everything_into_the_keycloak_namespace(self):
        docs = self.render("shared/keycloak-realms")
        self.assertEqual({d["metadata"].get("namespace") for d in docs}, {NAMESPACE})
        self.assertEqual({(d["kind"], d["metadata"]["name"]) for d in docs},
                         {("KeycloakRealmImport", "inkcast-staging"), ("KeycloakRealmImport", "inkcast-prod"),
                          ("Deployment", "mailpit"), ("Service", "mailpit"), ("NetworkPolicy", "mailpit")},
                         "no Namespace and no Secret: the lab's Keycloak owns the namespace, the secrets Job the Secrets")

    def test_the_sync_is_on_in_both_environments_reading_the_secret_the_job_writes(self):
        """D2: INKCAST_KEYCLOAK_SYNC_CLIENT_ID is the realm's service account and the secret comes from the file the
        secrets Job writes into inkcast-secrets (#323), mounted where the base mounts that Secret, beside the identity
        client's secret, so one Secret carries both halves of the contract."""
        realm = json.loads(REALM_FILE.read_text())
        sync_client = next(c["clientId"] for c in realm["clients"] if c["serviceAccountsEnabled"])
        for env in ENVIRONMENTS:
            with self.subTest(env=env):
                docs = self.render(f"environments/{env}")
                data = one(docs, "ConfigMap", "inkcast-config")["data"]
                self.assertEqual(data["INKCAST_KEYCLOAK_SYNC_CLIENT_ID"], sync_client)
                self.assertEqual(data["INKCAST_KEYCLOAK_SYNC_SECRET_FILE"], f"{SECRETS_DIR}/keycloak-sync-secret")
                self.assertEqual(data["INKCAST_LOGIN"], "oidc")
                self.assertIn("/realms/", data["OIDC_ISSUER"], "the sync needs a Keycloak issuer (libs/config.py)")
                pod = one(docs, "Deployment", "inkcast")["spec"]["template"]["spec"]
                (container,) = pod["containers"]
                env_values = {e["name"]: e.get("value") for e in container["env"]}
                self.assertEqual(env_values["OIDC_CLIENT_SECRET_FILE"], f"{SECRETS_DIR}/oidc-client-secret")
                mount = next(m for m in container["volumeMounts"] if m["mountPath"] == SECRETS_DIR)
                volume = next(v for v in pod["volumes"] if v["name"] == mount["name"])
                self.assertEqual(volume["secret"]["secretName"], "inkcast-secrets")


if __name__ == "__main__":
    unittest.main()
