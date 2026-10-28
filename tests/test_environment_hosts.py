# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Staging and production each have their own host, public URL and Keycloak realm on OpenShift Local (issue #305):
one cluster admits only one Route per host, and each environment signs people in against its own realm."""

from __future__ import annotations

import json
import pathlib
import subprocess
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENVIRONMENTS = {"staging": "staging", "production": "prod"}   # directory: the suffix of every name
APPS_DOMAIN = "apps-crc.testing"
KEYCLOAK = "https://keycloak.apps-crc.testing"


def render(environment: str) -> list[dict]:
    out = subprocess.run(["kubectl", "kustomize", str(ROOT / "deploy" / "kustomize" / "environments" / environment)],
                         capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def settings(environment: str) -> tuple[str, dict[str, str]]:
    docs = render(environment)
    route = next(d for d in docs if d["kind"] == "Route" and d["metadata"]["name"] == "inkcast")
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "inkcast-config")
    return route["spec"]["host"], config["data"]


class EnvironmentHosts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = {env: settings(env) for env in ENVIRONMENTS}

    def test_each_environment_names_itself_with_its_suffix(self):
        for env, side in ENVIRONMENTS.items():
            with self.subTest(env=env):
                host, data = self.rendered[env]
                self.assertEqual(host, f"inkcast-{side}.{APPS_DOMAIN}")
                self.assertEqual(data["INKCAST_PUBLIC_URL"], f"https://{host}")   # the Route serves the public URL
                self.assertEqual(data["OIDC_ISSUER"], f"{KEYCLOAK}/realms/inkcast-{side}")
                # The realm file's client (deploy/keycloak/realm-inkcast.json), as the ci-oidc overlays use it.
                realm = json.loads((ROOT / "deploy" / "keycloak" / "realm-inkcast.json").read_text())
                self.assertIn(data["OIDC_CLIENT_ID"], [c["clientId"] for c in realm["clients"]])

    def test_the_two_environments_share_nothing_a_person_reaches(self):
        (s_host, s), (p_host, p) = self.rendered["staging"], self.rendered["production"]
        self.assertNotEqual(s_host, p_host)
        self.assertNotEqual(s["INKCAST_PUBLIC_URL"], p["INKCAST_PUBLIC_URL"])
        self.assertNotEqual(s["OIDC_ISSUER"], p["OIDC_ISSUER"])

    def test_no_example_host_is_left(self):
        for env in ENVIRONMENTS:
            with self.subTest(env=env):
                self.assertNotIn("example.com", yaml.safe_dump_all(render(env)))


if __name__ == "__main__":
    unittest.main()
