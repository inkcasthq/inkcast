# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The cluster makes every generated Secret of a lab environment (E3.F3.9.S7, issue #323): ``inkcast env-secrets``
against a fake Kubernetes Secrets API (absent, one side, both agree, both disagree, races, API failures, and never a
value in its output), the in-cluster connection, and the launcher. The manifests that run it (the init container of
OpenFGA's pod, the ServiceAccount and the RBAC by name) are tests/test_env_secrets_manifests.py; the kind job
(scripts/dev/env-secrets-e2e.sh) proves the same on a cluster."""

from __future__ import annotations

import base64
import copy
import datetime
import io
import json
import pathlib
import re
import string
import tempfile
import unittest
from unittest import mock

import httpx2

from inkcast import env_secrets
from inkcast.env_secrets import Layout, connect, env_secrets_main
from inkcast.libs.config import load_config
from inkcast.libs.secrets import run_checks

SIDES = {"inkcast-staging": "staging", "inkcast-prod": "prod"}
ALPHANUMERIC = set(string.ascii_letters + string.digits)
HEX = set("0123456789abcdef")


def status(code: int, reason: str, message: str) -> httpx2.Response:
    return httpx2.Response(code, json={"kind": "Status", "apiVersion": "v1", "status": "Failure", "message": message,
                                       "reason": reason, "code": code})


class FakeKube:
    """The Secrets API of a cluster with the lab's namespaces, over httpx2.MockTransport: GET, POST (create) and
    PATCH (a JSON merge patch, conditional on metadata.resourceVersion as the API server makes it) of
    /api/v1/namespaces/<ns>/secrets[/<name>]. Every request is recorded. A test can refuse a (verb, namespace, name)
    as RBAC would, answer a request with another status, or run another writer just before a write lands."""

    PATH = re.compile(r"^/api/v1/namespaces/(?P<ns>[^/]+)/secrets(?:/(?P<name>[^/]+))?$")

    def __init__(self, namespaces=("inkcast-db", "keycloak", "inkcast-staging", "inkcast-prod")):
        self.namespaces = set(namespaces)
        self.objects: dict[tuple[str, str], dict] = {}
        self.requests: list[tuple[str, str, str, bytes]] = []  # method, namespace, name, body
        self.forbidden: set[tuple[str, str, str]] = set()  # verb (get, create, patch), namespace, name
        self.answers: dict[tuple[str, str, str], httpx2.Response] = {}
        self.before_write = None  # callable(method, namespace, name), once
        self.version = 0

    def seed(self, namespace: str, name: str, type_: str = "Opaque", labels: dict | None = None, **values: str) -> None:
        self.version += 1
        self.objects[(namespace, name)] = {
            "apiVersion": "v1", "kind": "Secret", "type": type_,
            "metadata": {"name": name, "namespace": namespace, "resourceVersion": str(self.version),
                         "labels": labels or {}},
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}}

    def values(self, namespace: str, name: str) -> dict[str, str] | None:
        obj = self.objects.get((namespace, name))
        return None if obj is None else {k: base64.b64decode(v).decode(errors="replace") for k, v in obj["data"].items()}

    def every_value(self) -> list[str]:
        """Every value stored, but a role's name (not a secret, and named in the output on purpose)."""
        return [v for key in self.objects for k, v in self.values(*key).items() if k != "username" and len(v) >= 6]

    def writes(self) -> list[tuple[str, str, str]]:
        return [(m, ns, name) for m, ns, name, _ in self.requests if m != "GET"]

    def client(self) -> httpx2.Client:
        return httpx2.Client(base_url="https://kube.test", transport=httpx2.MockTransport(self.handle))

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        match = self.PATH.match(request.url.path)
        assert match, request.url.path
        ns, name = match["ns"], match["name"]
        body = request.read()
        if request.method == "POST":
            name = json.loads(body)["metadata"]["name"]
        self.requests.append((request.method, ns, name, body))
        verb = {"GET": "get", "POST": "create", "PATCH": "patch"}[request.method]
        if (verb, ns, name) in self.answers:
            return self.answers[(verb, ns, name)]
        if (verb, ns, name) in self.forbidden:
            return status(403, "Forbidden", f'secrets "{name}" is forbidden: User "system:serviceaccount:inkcast-staging:'
                                            f'inkcast-env-secrets" cannot {verb} resource "secrets" in API group "" in the '
                                            f'namespace "{ns}"')
        if verb != "get" and self.before_write:
            hook, self.before_write = self.before_write, None
            hook(request.method, ns, name)
        obj = self.objects.get((ns, name))
        if verb == "get":
            return httpx2.Response(200, json=obj) if obj else status(404, "NotFound", f'secrets "{name}" not found')
        if verb == "create":
            if ns not in self.namespaces:
                return status(404, "NotFound", f'namespaces "{ns}" not found')
            if obj:
                return status(409, "AlreadyExists", f'secrets "{name}" already exists')
            new = json.loads(body)
            assert set(new) == {"apiVersion", "kind", "metadata", "type", "data"}, new
            assert "stringData" not in new
            self.version += 1
            new["metadata"] = {**new["metadata"], "namespace": ns, "resourceVersion": str(self.version)}
            self.objects[(ns, name)] = new
            return httpx2.Response(201, json=new)
        assert request.headers["content-type"] == "application/merge-patch+json"
        patch = json.loads(body)
        if not obj:
            return status(404, "NotFound", f'secrets "{name}" not found')
        if patch["metadata"]["resourceVersion"] != obj["metadata"]["resourceVersion"]:
            return status(409, "Conflict", f'Operation cannot be fulfilled on secrets "{name}": the object has been '
                                           "modified; please apply your changes to the latest version and try again")
        assert set(patch) == {"metadata", "data"} and set(patch["metadata"]) == {"resourceVersion"}, patch
        self.version += 1
        obj["data"].update(patch["data"])
        obj["metadata"]["resourceVersion"] = str(self.version)
        return httpx2.Response(200, json=obj)


class EnvSecretsCase(unittest.TestCase):
    def setUp(self):
        self.kube = FakeKube()
        tmp = tempfile.TemporaryDirectory(prefix="sa-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.namespace("inkcast-staging")

    def namespace(self, name: str) -> None:
        (self.root / "namespace").write_text(name + "\n")

    def run_main(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = env_secrets_main([], root=self.root, environ={}, connect=lambda environ, root: self.kube.client(),
                                out=out, err=err)
        self.assert_no_value(out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def assert_no_value(self, text: str) -> None:
        for value in self.kube.every_value():
            self.assertNotIn(value, text, "a Secret's value in the output")

    def layout(self, namespace: str = "inkcast-staging") -> Layout:
        return Layout.for_namespace(namespace)

    def seed_db_pair(self, side: str, role_pw: str, app_pw: str | None = None) -> None:
        layout = self.layout(f"inkcast-{side}")
        self.kube.seed("inkcast-db", f"inkcast-pg-openfga-{side}", "kubernetes.io/basic-auth",
                       username=f"openfga_{side}", password=role_pw)
        app_pw = role_pw if app_pw is None else app_pw
        self.kube.seed(f"inkcast-{side}", "openfga-db-app", username=f"openfga_{side}", password=app_pw,
                       uri=layout.uri(app_pw))


class FreshSide(EnvSecretsCase):
    def test_a_fresh_side_gets_every_secret_once_and_in_agreement(self):
        for namespace, side in SIDES.items():
            with self.subTest(side=side):
                self.namespace(namespace)
                code, out, err = self.run_main()
                self.assertEqual(code, 0, err)
                role = self.kube.values("inkcast-db", f"inkcast-pg-openfga-{side}")
                app = self.kube.values(namespace, "openfga-db-app")
                realm = self.kube.values("keycloak", f"inkcast-{side}-realm")
                inkcast = self.kube.values(namespace, "inkcast-secrets")
                self.assertEqual(set(role), {"username", "password"})
                self.assertEqual(set(app), {"uri", "username", "password"})
                self.assertEqual(set(realm), {"identity-client-secret", "sync-client-secret"})
                self.assertEqual(set(inkcast), {"secret-key", "fga-api-token", "oidc-client-secret", "keycloak-sync-secret"})
                # The three pairs hold one value each.
                self.assertEqual(role["username"], f"openfga_{side}")
                self.assertEqual((app["username"], app["password"]), (role["username"], role["password"]))
                self.assertEqual(app["uri"], f"postgres://openfga_{side}:{role['password']}@inkcast-pg-rw.inkcast-db.svc:5432/"
                                             f"openfga_{side}?sslmode=require")
                self.assertEqual(inkcast["oidc-client-secret"], realm["identity-client-secret"])
                self.assertEqual(inkcast["keycloak-sync-secret"], realm["sync-client-secret"])
                self.assertNotEqual(realm["identity-client-secret"], realm["sync-client-secret"])
                # The types and labels the consumers need: CloudNativePG reads basic-auth and reloads on the label.
                db = self.kube.objects[("inkcast-db", f"inkcast-pg-openfga-{side}")]
                self.assertEqual(db["type"], "kubernetes.io/basic-auth")
                self.assertEqual(db["metadata"]["labels"], {"app.kubernetes.io/part-of": "inkcast", "cnpg.io/reload": "true"})
                for key in ((namespace, "openfga-db-app"), ("keycloak", f"inkcast-{side}-realm"), (namespace, "inkcast-secrets")):
                    self.assertEqual(self.kube.objects[key]["type"], "Opaque", key)
                    self.assertEqual(self.kube.objects[key]["metadata"]["labels"], {"app.kubernetes.io/part-of": "inkcast"}, key)
                for ref in (f"inkcast-db/inkcast-pg-openfga-{side}", f"{namespace}/openfga-db-app",
                            f"keycloak/inkcast-{side}-realm", f"{namespace}/inkcast-secrets"):
                    self.assertIn(f"{ref}: created", out)

    def test_each_value_has_the_length_and_alphabet_its_consumer_needs(self):
        """The role's password: 48 hex (in the URI with nothing to escape). Each Keycloak client secret: Keycloak's
        own for a client-secret client, 32 alphanumerics (KeycloakModelUtils.generateSecret at 26.6.7), nothing to
        escape in the realm JSON its placeholder lands in. OpenFGA's preshared key: 64 alphanumerics, no comma (the
        server splits OPENFGA_AUTHN_PRESHARED_KEYS on commas). Inkcast's key: what the posture check passes."""
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)
        password = self.kube.values("inkcast-db", "inkcast-pg-openfga-staging")["password"]
        self.assertEqual(len(password), 48)
        self.assertLessEqual(set(password), HEX)
        realm = self.kube.values("keycloak", "inkcast-staging-realm")
        for key in ("identity-client-secret", "sync-client-secret"):
            self.assertEqual(len(realm[key]), 32, key)
            self.assertLessEqual(set(realm[key]), ALPHANUMERIC, key)
        inkcast = self.kube.values("inkcast-staging", "inkcast-secrets")
        self.assertEqual(len(inkcast["fga-api-token"]), 64)
        self.assertLessEqual(set(inkcast["fga-api-token"]), ALPHANUMERIC)
        self.assertGreaterEqual(len(inkcast["secret-key"]), 64)

    def test_inkcast_s_key_always_passes_the_production_posture_check(self):
        """A key from the CSPRNG can still contain a word the check treats as a placeholder ("test", "1234"...):
        the generator draws again, so production never refuses a generated key. Forced here: the first draws hold
        the words, the last is clean."""
        draws = iter(["a" * 30 + "TeSt" + "b" * 30, "x" * 20 + "1234" + "y" * 40, "Q" * 64,
                      "N4hW_v2-Lq8KzR0tYbXe6JmUcP1sGd9fAoI3iVrHn5w7DlEyBkTgZaMxCuOjSpFQ"])
        with mock.patch.object(env_secrets.secrets, "token_urlsafe", side_effect=lambda n: next(draws)):
            key = env_secrets.make_server_key()
        self.assertEqual(key, "N4hW_v2-Lq8KzR0tYbXe6JmUcP1sGd9fAoI3iVrHn5w7DlEyBkTgZaMxCuOjSpFQ")
        for _ in range(50):
            key = env_secrets.make_server_key()
            config = load_config({"INKCAST_SECRET_KEY": key, "INKCAST_ENV": "production",
                                  "INKCAST_PUBLIC_URL": "https://inkcast.example.com"})
            (check,) = [c for c in run_checks(config, environ={}) if c.id == "secret-key-strength"]
            self.assertEqual(check.status, "pass", check.detail)

    def test_every_value_comes_from_the_csprng_and_differs(self):
        self.assertNotIn("import random", pathlib.Path(env_secrets.__file__).read_text())
        for namespace in SIDES:
            self.namespace(namespace)
            self.assertEqual(self.run_main()[0], 0)
        values = [v for key in self.kube.objects for k, v in self.kube.values(*key).items() if k not in ("username", "uri")]
        # Per side: the password on both sides of its pair, the two client secrets on both sides of theirs, and
        # Inkcast's key and OpenFGA's: 8 values, 5 of them distinct; nothing shared between the sides.
        self.assertEqual(len(values), 2 * 8)
        self.assertEqual(len(set(values)), 2 * 5)

    def test_a_second_run_writes_nothing(self):
        self.assertEqual(self.run_main()[0], 0)
        before = copy.deepcopy(self.kube.objects)
        self.kube.requests.clear()
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kube.writes(), [])
        self.assertEqual(self.kube.objects, before)
        self.assertEqual(out.count(": kept"), 4, out)

    def test_it_touches_only_its_own_side_s_secrets(self):
        """What RBAC grants by name: the four Secrets of this side, and nothing of the other side."""
        self.assertEqual(self.run_main()[0], 0)
        touched = {(ns, name) for _, ns, name, _ in self.kube.requests}
        self.assertEqual(touched, {("inkcast-db", "inkcast-pg-openfga-staging"), ("inkcast-staging", "openfga-db-app"),
                                   ("keycloak", "inkcast-staging-realm"), ("inkcast-staging", "inkcast-secrets")})


class OneSidePresent(EnvSecretsCase):
    def test_the_role_s_password_is_copied_to_the_app_secret(self):
        self.kube.seed("inkcast-db", "inkcast-pg-openfga-staging", "kubernetes.io/basic-auth",
                       username="openfga_staging", password="a1" * 24)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        app = self.kube.values("inkcast-staging", "openfga-db-app")
        self.assertEqual(app["password"], "a1" * 24)
        self.assertEqual(app["uri"], self.layout().uri("a1" * 24))
        self.assertIn("from inkcast-db/inkcast-pg-openfga-staging", out)
        self.assertNotIn(("POST", "inkcast-db", "inkcast-pg-openfga-staging"), self.kube.writes())

    def test_the_app_secret_s_password_is_copied_to_the_role(self):
        """The shared database's namespace rebuilt: the role gets back the password OpenFGA connects with."""
        pw = "b2" * 24
        self.kube.seed("inkcast-staging", "openfga-db-app", username="openfga_staging", password=pw,
                       uri=self.layout().uri(pw))
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kube.values("inkcast-db", "inkcast-pg-openfga-staging"),
                         {"username": "openfga_staging", "password": pw})
        self.assertEqual(self.kube.objects[("inkcast-db", "inkcast-pg-openfga-staging")]["type"], "kubernetes.io/basic-auth")
        self.assertIn("from inkcast-staging/openfga-db-app", out)

    def test_a_password_that_needs_escaping_is_escaped_in_the_uri(self):
        """A role password written by hand may hold URI delimiters: the copied URI percent-encodes it."""
        self.kube.seed("inkcast-db", "inkcast-pg-openfga-staging", "kubernetes.io/basic-auth",
                       username="openfga_staging", password="p@ss/w:rd?#%")
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.kube.values("inkcast-staging", "openfga-db-app")["uri"],
                         "postgres://openfga_staging:p%40ss%2Fw%3Ard%3F%23%25@inkcast-pg-rw.inkcast-db.svc:5432/"
                         "openfga_staging?sslmode=require")

    def test_the_realm_s_client_secrets_are_copied_to_inkcast(self):
        self.kube.seed("keycloak", "inkcast-staging-realm", **{"identity-client-secret": "I" * 32, "sync-client-secret": "S" * 32})
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        inkcast = self.kube.values("inkcast-staging", "inkcast-secrets")
        self.assertEqual((inkcast["oidc-client-secret"], inkcast["keycloak-sync-secret"]), ("I" * 32, "S" * 32))
        self.assertEqual(self.kube.values("keycloak", "inkcast-staging-realm"),
                         {"identity-client-secret": "I" * 32, "sync-client-secret": "S" * 32}, "kept as it was")
        self.assertEqual(self.kube.writes().count(("PATCH", "keycloak", "inkcast-staging-realm")), 0)

    def test_inkcast_s_client_secrets_are_copied_to_the_realm(self):
        """The keycloak side lost: the realm Secret gets back what Inkcast (and the imported realm) hold."""
        self.kube.seed("inkcast-staging", "inkcast-secrets", **{"secret-key": "k" * 64, "fga-api-token": "f" * 64,
                       "oidc-client-secret": "O" * 32, "keycloak-sync-secret": "Y" * 32})
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kube.values("keycloak", "inkcast-staging-realm"),
                         {"identity-client-secret": "O" * 32, "sync-client-secret": "Y" * 32})
        self.assertNotIn("inkcast-secrets", " ".join(f"{m} {ns}/{n}" for m, ns, n in self.kube.writes()))

    def test_a_hand_made_secret_gets_only_the_keys_it_lacks(self):
        """The lab today: inkcast-secrets made by hand without keycloak-sync-secret, no realm Secret yet. Its keys are
        kept as they are; the missing one is added by a merge patch conditional on the version read; the realm gets
        the identity secret Inkcast already holds and the new sync secret."""
        self.seed_db_pair("staging", "c3" * 24)
        self.kube.seed("inkcast-staging", "inkcast-secrets", **{"secret-key": "k" * 64, "fga-api-token": "f" * 64,
                       "oidc-client-secret": "O" * 32})
        version = self.kube.objects[("inkcast-staging", "inkcast-secrets")]["metadata"]["resourceVersion"]
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        inkcast = self.kube.values("inkcast-staging", "inkcast-secrets")
        realm = self.kube.values("keycloak", "inkcast-staging-realm")
        self.assertEqual({k: inkcast[k] for k in ("secret-key", "fga-api-token", "oidc-client-secret")},
                         {"secret-key": "k" * 64, "fga-api-token": "f" * 64, "oidc-client-secret": "O" * 32})
        self.assertEqual(realm, {"identity-client-secret": "O" * 32, "sync-client-secret": inkcast["keycloak-sync-secret"]})
        (patch,) = [body for m, ns, name, body in self.kube.requests if m == "PATCH"]
        patch = json.loads(patch)
        self.assertEqual(patch["metadata"], {"resourceVersion": version})
        self.assertEqual(set(patch["data"]), {"keycloak-sync-secret"}, "only the missing key")
        self.assertEqual(self.kube.writes(), [("POST", "keycloak", "inkcast-staging-realm"),
                                              ("PATCH", "inkcast-staging", "inkcast-secrets")])
        self.assertIn("added keycloak-sync-secret", out)


class BothSides(EnvSecretsCase):
    def test_a_database_pair_that_disagrees_fails_naming_it_and_writes_nothing(self):
        self.seed_db_pair("staging", "d4" * 24, app_pw="e5" * 24)
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("inkcast-staging/openfga-db-app", err)
        self.assertIn("inkcast-db/inkcast-pg-openfga-staging", err)
        self.assertIn("openfga_staging", err)
        self.assertRegex(err, "different passwords")
        self.assertEqual(self.kube.writes(), [], "nothing written, not even the Secrets that are simply absent")

    def test_a_client_secret_pair_that_disagrees_fails_naming_it_and_writes_nothing(self):
        self.seed_db_pair("staging", "f6" * 24)
        self.kube.seed("keycloak", "inkcast-staging-realm", **{"identity-client-secret": "I" * 32, "sync-client-secret": "S" * 32})
        self.kube.seed("inkcast-staging", "inkcast-secrets", **{"secret-key": "k" * 64, "fga-api-token": "f" * 64,
                       "oidc-client-secret": "Z" * 32, "keycloak-sync-secret": "S" * 32})
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("keycloak/inkcast-staging-realm identity-client-secret", err)
        self.assertIn("inkcast-staging/inkcast-secrets oidc-client-secret", err)
        self.assertNotIn("sync-client-secret", err, "the agreeing pair is not named")
        self.assertEqual(self.kube.writes(), [])

    def test_every_problem_is_named_in_one_run(self):
        self.seed_db_pair("staging", "a7" * 24, app_pw="b8" * 24)
        self.kube.seed("keycloak", "inkcast-staging-realm", **{"identity-client-secret": "I" * 32, "sync-client-secret": "S" * 32})
        self.kube.seed("inkcast-staging", "inkcast-secrets", **{"oidc-client-secret": "J" * 32, "keycloak-sync-secret": "T" * 32})
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        for needle in ("different passwords", "identity-client-secret", "sync-client-secret"):
            self.assertIn(needle, err)
        self.assertEqual(self.kube.writes(), [])

    def test_an_agreeing_environment_is_kept_as_it_is(self):
        self.seed_db_pair("staging", "c9" * 24)
        self.kube.seed("keycloak", "inkcast-staging-realm", **{"identity-client-secret": "I" * 32, "sync-client-secret": "S" * 32})
        self.kube.seed("inkcast-staging", "inkcast-secrets", **{"secret-key": "k" * 64, "fga-api-token": "f" * 64,
                       "oidc-client-secret": "I" * 32, "keycloak-sync-secret": "S" * 32})
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kube.writes(), [])

    def test_a_side_that_is_not_what_its_consumer_reads_is_named_and_not_copied(self):
        cases = {
            "the role Secret names another role": (
                lambda: self.kube.seed("inkcast-db", "inkcast-pg-openfga-staging", "kubernetes.io/basic-auth",
                                       username="openfga_prod", password="a" * 48),
                "inkcast-db/inkcast-pg-openfga-staging"),
            "the role Secret has no password": (
                lambda: self.kube.seed("inkcast-db", "inkcast-pg-openfga-staging", "kubernetes.io/basic-auth",
                                       username="openfga_staging"),
                "inkcast-db/inkcast-pg-openfga-staging"),
            "the app Secret points at another database": (
                lambda: self.kube.seed("inkcast-staging", "openfga-db-app", username="openfga_staging", password="a" * 48,
                                       uri="postgres://openfga_staging:" + "a" * 48 + "@openfga-db-rw:5432/openfga"),
                "inkcast-staging/openfga-db-app"),
            "the app Secret drops sslmode": (
                lambda: self.kube.seed("inkcast-staging", "openfga-db-app", username="openfga_staging", password="a" * 48,
                                       uri=self.layout().uri("a" * 48).removesuffix("?sslmode=require")),
                "inkcast-staging/openfga-db-app"),
            "an empty client secret": (
                lambda: self.kube.seed("keycloak", "inkcast-staging-realm", **{"identity-client-secret": ""}),
                "keycloak/inkcast-staging-realm identity-client-secret is empty"),
            "an empty key": (
                lambda: self.kube.seed("inkcast-staging", "inkcast-secrets", **{"secret-key": ""}),
                "inkcast-staging/inkcast-secrets secret-key is empty"),
        }
        for case, (seed, needle) in cases.items():
            with self.subTest(case=case):
                self.kube = FakeKube()
                seed()
                code, _, err = self.run_main()
                self.assertEqual(code, 1, case)
                self.assertIn(needle, err)
                self.assertEqual(self.kube.writes(), [], case)

    def test_a_value_that_is_not_text_is_named(self):
        self.kube.seed("inkcast-staging", "inkcast-secrets")
        self.kube.objects[("inkcast-staging", "inkcast-secrets")]["data"]["fga-api-token"] = base64.b64encode(b"\xff\xfe").decode()
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("inkcast-staging/inkcast-secrets fga-api-token is not UTF-8 text", err)
        self.assertEqual(self.kube.writes(), [])


class Races(EnvSecretsCase):
    """Two pods of the environment starting at once (a rollout's surge, a restart): a write that loses (409) starts
    over from the reads, and both end on the winner's values."""

    def test_a_create_that_loses_a_race_reads_again_and_copies_the_winner(self):
        def other_writer(method, ns, name):
            self.kube.seed("inkcast-db", "inkcast-pg-openfga-staging", "kubernetes.io/basic-auth",
                           username="openfga_staging", password="0f" * 24)
        self.kube.before_write = other_writer
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kube.values("inkcast-staging", "openfga-db-app")["password"], "0f" * 24)
        self.assertEqual(self.kube.values("inkcast-db", "inkcast-pg-openfga-staging")["password"], "0f" * 24)
        posts = [w for w in self.kube.writes() if w == ("POST", "inkcast-db", "inkcast-pg-openfga-staging")]
        self.assertEqual(len(posts), 1, "the losing create is not retried blindly")

    def test_a_patch_that_loses_a_race_reads_again(self):
        self.seed_db_pair("staging", "c3" * 24)
        self.kube.seed("keycloak", "inkcast-staging-realm", **{"identity-client-secret": "I" * 32, "sync-client-secret": "S" * 32})
        self.kube.seed("inkcast-staging", "inkcast-secrets", **{"secret-key": "k" * 64, "oidc-client-secret": "I" * 32,
                       "keycloak-sync-secret": "S" * 32})

        def other_writer(method, ns, name):
            obj = self.kube.objects[("inkcast-staging", "inkcast-secrets")]
            obj["data"]["fga-api-token"] = base64.b64encode(b"W" * 64).decode()
            self.kube.version += 1
            obj["metadata"]["resourceVersion"] = str(self.kube.version)
        self.kube.before_write = other_writer
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kube.values("inkcast-staging", "inkcast-secrets")["fga-api-token"], "W" * 64,
                         "the other writer's value is kept, never overwritten")
        self.assertEqual([w for w in self.kube.writes() if w[0] == "PATCH"], [("PATCH", "inkcast-staging", "inkcast-secrets")])

    def test_a_race_that_never_settles_fails_after_a_few_rounds(self):
        self.kube.answers[("create", "inkcast-db", "inkcast-pg-openfga-staging")] = status(
            409, "AlreadyExists", 'secrets "inkcast-pg-openfga-staging" already exists')
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("Another writer kept changing these Secrets", err)
        self.assertEqual(len([w for w in self.kube.writes() if w[0] == "POST"]), env_secrets.ATTEMPTS)


class Failures(EnvSecretsCase):
    def test_an_api_failure_while_reading_is_the_api_s_error_never_a_disagreement(self):
        """An agreeing environment, and one read refused or failing: the run stops with the API's own answer. It never
        reads the failure as an absent Secret (which would generate a new password and cut OpenFGA off) or as a
        disagreement."""
        refs = [("inkcast-db", "inkcast-pg-openfga-staging"), ("inkcast-staging", "openfga-db-app"),
                ("keycloak", "inkcast-staging-realm"), ("inkcast-staging", "inkcast-secrets")]
        for ns, name in refs:
            for answer in (status(403, "Forbidden", f'secrets "{name}" is forbidden'),
                           status(500, "InternalError", "etcdserver: request timed out")):
                with self.subTest(secret=f"{ns}/{name}", code=answer.status_code):
                    self.kube = FakeKube()
                    self.seed_db_pair("staging", "c3" * 24)
                    self.kube.seed("keycloak", "inkcast-staging-realm",
                                   **{"identity-client-secret": "I" * 32, "sync-client-secret": "S" * 32})
                    self.kube.seed("inkcast-staging", "inkcast-secrets", **{"secret-key": "k" * 64, "fga-api-token": "f" * 64,
                                   "oidc-client-secret": "I" * 32, "keycloak-sync-secret": "S" * 32})
                    self.kube.answers[("get", ns, name)] = answer
                    code, out, err = self.run_main()
                    self.assertEqual(code, 1)
                    self.assertIn(f"read Secret {ns}/{name}: HTTP {answer.status_code}", err)
                    for misread in ("disagree", "different", "created", "kept"):
                        self.assertNotIn(misread, err + out)
                    self.assertEqual(self.kube.writes(), [])

    def test_a_refused_write_names_the_kustomization_that_grants_it(self):
        for ns, name, grant in (("inkcast-db", "inkcast-pg-openfga-staging", "deploy/kustomize/shared/inkcast-db"),
                                ("keycloak", "inkcast-staging-realm", "deploy/kustomize/shared/keycloak-access"),
                                ("inkcast-staging", "inkcast-secrets", "deploy/kustomize/components/env-secrets")):
            with self.subTest(secret=f"{ns}/{name}"):
                self.kube = FakeKube()
                self.kube.forbidden.add(("create", ns, name))
                code, _, err = self.run_main()
                self.assertEqual(code, 1)
                self.assertIn(f"create Secret {ns}/{name}: HTTP 403 Forbidden", err)
                self.assertIn(grant, err)

    def test_a_missing_namespace_is_named(self):
        self.kube = FakeKube(namespaces=("inkcast-db", "inkcast-staging"))
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn('namespaces "keycloak" not found', err)

    def test_an_unreachable_api_fails_closed(self):
        def down(request):
            raise httpx2.ConnectError("[Errno 111] Connection refused")
        out, err = io.StringIO(), io.StringIO()
        code = env_secrets_main([], root=self.root, environ={}, out=out, err=err,
                                connect=lambda environ, root: httpx2.Client(base_url="https://kube.test",
                                                                            transport=httpx2.MockTransport(down)))
        self.assertEqual(code, 1)
        self.assertIn("can't reach the Kubernetes API", err.getvalue())

    def test_a_namespace_that_is_not_an_environment_is_refused_before_any_call(self):
        for namespace in ("inkcast", "inkcast-openfga", "default", ""):
            with self.subTest(namespace=namespace):
                self.namespace(namespace)
                code, _, err = self.run_main()
                self.assertEqual(code, 1)
                self.assertIn("inkcast-staging or inkcast-prod", err)
                self.assertEqual(self.kube.requests, [])


class InCluster(unittest.TestCase):
    """The pod's own credentials: the projected token, the cluster's CA and the namespace, at the path the init
    container mounts them (the standard one), and the API server's address from the kubelet's variables."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="sa-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "token").write_text("eyJ.header.sig\n")
        (self.root / "namespace").write_text("inkcast-staging")
        (self.root / "ca.crt").write_text(self_signed_pem())

    def test_the_client_talks_to_the_api_server_with_the_pod_s_token(self):
        for host, url in (("10.96.0.1", "https://10.96.0.1:6443"), ("fd00::1", "https://[fd00::1]:6443")):
            with self.subTest(host=host):
                with connect({"KUBERNETES_SERVICE_HOST": host, "KUBERNETES_SERVICE_PORT": "6443",
                              "HTTPS_PROXY": "http://proxy.example:3128"}, self.root) as client:
                    self.assertEqual(str(client.base_url).rstrip("/"), url)
                    self.assertEqual(client.headers["authorization"], "Bearer eyJ.header.sig")
                    self.assertFalse(client.trust_env, "the API server is reached directly, never through a proxy")

    def test_outside_a_pod_it_refuses(self):
        out, err = io.StringIO(), io.StringIO()
        code = env_secrets_main([], root=self.root, environ={}, out=out, err=err)
        self.assertEqual(code, 1)
        self.assertIn("KUBERNETES_SERVICE_HOST", err.getvalue())
        (self.root / "token").unlink()
        code = env_secrets_main([], root=self.root, environ={"KUBERNETES_SERVICE_HOST": "10.96.0.1"}, out=out, err=err)
        self.assertEqual(code, 1)
        self.assertIn(str(self.root / "token"), err.getvalue())

    def test_the_launcher_dispatches_env_secrets(self):
        from inkcast import launcher
        with mock.patch.object(env_secrets, "env_secrets_main", return_value=0) as called:
            with self.assertRaises(SystemExit) as done:
                launcher.main(["env-secrets"])
        self.assertEqual(done.exception.code, 0)
        called.assert_called_once_with([])


def self_signed_pem() -> str:
    """A CA certificate the SSL context can load (the cluster's kube-root-ca.crt in a pod)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kube-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


if __name__ == "__main__":
    unittest.main()
