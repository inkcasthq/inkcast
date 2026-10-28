# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #336 (issue #323, ``inkcast env-secrets``): what tests/test_env_secrets.py and
tests/test_env_secrets_manifests.py leave unproved, each case failing on the code it was written against: 57ebd3e,
whose src, deploy and tests are 768217a's."""

from __future__ import annotations

import base64
import json
import shutil
import unittest

import httpx2
from test_env_secrets import EnvSecretsCase, FakeKube, status
from test_openfga_component import one, pod_spec, render


class QuotingWebhook(FakeKube):
    """An admission webhook that denies one write and quotes the object it denied in its message, as a policy engine's
    message template may (the AdmissionReview carries the whole object, and for a patch the stored one too): the
    value comes back to the client inside the API server's Status."""

    def __init__(self, verb: str, namespace: str, name: str, key: str, encoded: bool):
        super().__init__()
        self.target, self.key, self.encoded = (verb, namespace, name), key, encoded
        self.quoted: str | None = None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        match = self.PATH.match(request.url.path)
        verb = {"GET": "get", "POST": "create", "PATCH": "patch"}[request.method]
        name = json.loads(request.read())["metadata"]["name"] if verb == "create" else match["name"]
        if (verb, match["ns"], name) != self.target:
            return super().handle(request)
        # The object as admission sees it: the stored data, with the request's data merged over it.
        stored = (self.objects.get((match["ns"], name)) or {}).get("data", {})
        data = {**stored, **json.loads(request.read())["data"]}
        self.quoted = base64.b64decode(data[self.key]).decode()
        shown = data[self.key] if self.encoded else self.quoted
        return status(403, "Forbidden", f'admission webhook "secrets.policy.example" denied the request: '
                                        f"data.{self.key}={shown} is not allowed here")


class ApiMessagesNeverCarryAValue(EnvSecretsCase):
    """AC4: no value in the run's output. _refused prints the Status message the API server returns, and a webhook's
    denial is such a message: a value it quotes, as text or as the base64 the API carries, must not reach stderr."""

    CASES = {
        "the role's new password, in a create": ("create", "inkcast-db", "inkcast-pg-openfga-staging", "password"),
        "a key added by a patch": ("patch", "inkcast-staging", "inkcast-secrets", "keycloak-sync-secret"),
        "a key the Secret already held, in a patch": ("patch", "inkcast-staging", "inkcast-secrets", "secret-key"),
    }

    def test_a_webhook_that_quotes_the_secret_it_denies_never_puts_a_value_in_the_output(self):
        for case, target in self.CASES.items():
            for encoded in (False, True):
                with self.subTest(case=case, encoded=encoded):
                    self.kube = QuotingWebhook(*target, encoded=encoded)
                    if target[0] == "patch":
                        self.kube.seed("inkcast-staging", "inkcast-secrets", **{
                            "secret-key": "Kx7" * 20, "fga-api-token": "f" * 64, "oidc-client-secret": "O" * 32})
                    code, out, err = self.run_main()
                    self.assertEqual(code, 1)
                    self.assertIn('admission webhook "secrets.policy.example" denied the request', err,
                                  "the API's reason still reaches the operator")
                    value = self.kube.quoted
                    self.assertTrue(value)
                    for form in (value, base64.b64encode(value.encode()).decode()):
                        self.assertNotIn(form, out + err)


class TheMigrateJobWaitsForEnvSecrets(EnvSecretsCase):
    """The environments' openfga-migrate Job reads openfga-db-app, which nobody makes before the apply any more: the
    init container env-secrets creates it, and only once every read it does succeeds, Keycloak's namespace included
    (shared/keycloak-access applied, Keycloak installed). A Job's activeDeadlineSeconds counts from the Job's start,
    with its pod waiting on the missing Secret (CreateContainerConfigError) or not; once it passes, the Job is Failed
    (DeadlineExceeded) for good, and only a person deleting it and applying again runs the migration: OpenFGA's
    server stays NOT_SERVING on the old schema. Real failures stay bounded by backoffLimit (restartPolicy OnFailure
    counts container restarts)."""

    def test_openfga_db_app_waits_on_keycloak_s_namespace(self):
        self.kube.forbidden.add(("get", "keycloak", "inkcast-staging-realm"))
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("deploy/kustomize/shared/keycloak-access", err)
        self.assertNotIn(("inkcast-staging", "openfga-db-app"), self.kube.objects, "the migrate Job's input is not made")

    @unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
    def test_the_migrate_job_has_no_deadline_while_env_secrets_makes_its_secret(self):
        for target in ("environments/staging", "environments/production", "overlays/ci-env-secrets"):
            with self.subTest(target=target):
                job = one(render(target), "Job", "openfga-migrate")
                (container,) = pod_spec(job)["containers"]
                uri = {e["name"]: e for e in container["env"]}["OPENFGA_DATASTORE_URI"]
                self.assertEqual(uri["valueFrom"]["secretKeyRef"]["name"], "openfga-db-app")
                self.assertNotIn("activeDeadlineSeconds", job["spec"], "a deadline fails the Job for good")
                self.assertEqual((job["spec"]["backoffLimit"], pod_spec(job)["restartPolicy"]), (10, "OnFailure"))
