"""The Keycloak realm as code (deploy/keycloak/realm-inkcast.json, E9.F9.1.S1): no secret or person in Git, every
placeholder fed by the deployment, and the security settings ADR-0006 relies on."""

import json
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
REALM = ROOT / "deploy" / "keycloak" / "realm-inkcast.json"
DEPLOYMENT = ROOT / "deploy" / "keycloak" / "keycloak.yaml"


class Realm(unittest.TestCase):
    def setUp(self):
        self.text = REALM.read_text()
        self.realm = json.loads(self.text)
        self.client = next(c for c in self.realm["clients"] if c["clientId"] == "inkcast-identity")

    def test_no_secret_and_no_person_in_git(self):
        self.assertNotIn("users", self.realm, "people are created at runtime, never committed")
        self.assertEqual(self.client["secret"], "${KC_INKCAST_CLIENT_SECRET}")
        for key in ("smtpServer", "identityProviders"):
            for item in ([self.realm.get(key)] if isinstance(self.realm.get(key), dict) else self.realm.get(key, [])):
                for field in ("password", "clientSecret"):
                    value = str((item or {}).get(field, (item or {}).get("config", {}).get(field, "")))
                    self.assertTrue(not value or value.startswith("${"), f"{key}.{field} must be a placeholder")

    def test_every_placeholder_is_fed_by_the_deployment(self):
        used = set(re.findall(r"\$\{([A-Z0-9_]+)\}", self.text))
        self.assertTrue({"KC_INKCAST_CLIENT_SECRET", "INKCAST_PUBLIC_URL", "KC_SMTP_HOST", "KC_SMTP_PASSWORD"} <= used)
        docs = list(yaml.safe_load_all(DEPLOYMENT.read_text()))
        keycloak = next(d for d in docs if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "keycloak")
        container = keycloak["spec"]["template"]["spec"]["containers"][0]
        given = {e["name"] for e in container["env"]}
        kustomization = yaml.safe_load((DEPLOYMENT.parent / "kustomization.yaml").read_text())
        for ref in container.get("envFrom", []):                     # keys of the generated ConfigMaps it loads
            generator = next(g for g in kustomization["configMapGenerator"] if g["name"] == ref["configMapRef"]["name"])
            given |= {lit.split("=", 1)[0] for lit in generator.get("literals", [])}
        self.assertEqual(used - given, set(), "an unfilled placeholder stays literal")
        self.assertIn("--import-realm", container["args"])

    def test_required_actions_keep_keycloak_defaults_and_require_the_terms(self):
        """Listing required actions replaces Keycloak's defaults, so the ones Inkcast relies on must all be here."""
        actions = {a["alias"]: a for a in self.realm["requiredActions"]}
        for alias in ("VERIFY_EMAIL", "UPDATE_PASSWORD", "CONFIGURE_TOTP", "webauthn-register", "TERMS_AND_CONDITIONS"):
            self.assertTrue(actions[alias]["enabled"], alias)
        self.assertTrue(actions["TERMS_AND_CONDITIONS"]["defaultAction"], "every new account accepts the terms")
        self.assertEqual(self.realm["smtpServer"]["password"], "${KC_SMTP_PASSWORD}")

    def test_the_client_is_a_confidential_pkce_code_flow_with_exact_redirects(self):
        c = self.client
        self.assertFalse(c["publicClient"])
        self.assertTrue(c["standardFlowEnabled"])
        self.assertFalse(c["implicitFlowEnabled"])
        self.assertFalse(c["directAccessGrantsEnabled"], "no password grant: people sign in on Keycloak's page")
        self.assertEqual(c["attributes"]["pkce.code.challenge.method"], "S256")
        self.assertEqual(c["redirectUris"], ["${INKCAST_PUBLIC_URL}/auth/callback"])
        self.assertTrue(all("*" not in u for u in c["redirectUris"] + c["webOrigins"]), "no wildcard redirects")

    def test_roles_and_account_protection(self):
        r = self.realm
        roles = {x["name"]: x for x in r["roles"]["realm"]}
        self.assertIn("inkcast-admin", roles)
        self.assertEqual(r["defaultRole"]["name"], "default-roles-inkcast")
        self.assertIn("inkcast-user", roles["default-roles-inkcast"]["composites"]["realm"])
        self.assertTrue(r["bruteForceProtected"])
        self.assertTrue(r["verifyEmail"], "account linking by email is only safe with verified emails (E9 risks)")
        self.assertTrue(r["registrationEmailAsUsername"])
        self.assertGreaterEqual(int(re.search(r"length\((\d+)\)", r["passwordPolicy"]).group(1)), 12)
        self.assertTrue(r["revokeRefreshToken"])
        self.assertEqual(r["sslRequired"], "external")
        self.assertLessEqual(r["accessTokenLifespan"], 300)


if __name__ == "__main__":
    unittest.main()
