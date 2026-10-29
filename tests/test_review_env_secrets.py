# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #336 (issue #323, ``inkcast env-secrets``): what tests/test_env_secrets.py and
tests/test_env_secrets_manifests.py leave unproved, each case failing on the code it was written against: 57ebd3e,
whose src, deploy and tests are 768217a's. The integrator's cases for the review's concerns follow: C1 (each side
creates only its own Secrets: an admission policy), C4 (what each account is granted), C5 (a patch race), C7 (an
admission denial is not a missing Role)."""

from __future__ import annotations

import base64
import json
import pathlib
import re
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


class TheMigrationWaitsForEnvSecrets(EnvSecretsCase):
    """The environments' schema migration reads openfga-db-app, which nobody makes before the apply any more: the
    init container env-secrets creates it, and only once every read it does succeeds, Keycloak's namespace included
    (shared/keycloak-access applied, Keycloak installed). Until E3.F3.9.S8 (issue #337) the migration was a Job whose
    activeDeadlineSeconds counted from the Job's start, with its pod waiting on the missing Secret or not; past it the
    Job was Failed for good and only a person deleting it and applying again ran the migration. Now the migration is
    the init container `migrate` of the same pod, ordered after env-secrets: the kubelet starts env-secrets first and
    starts migrate only once it has succeeded, resolving migrate's secretKeyRef at that moment, so the Secret this run
    wrote is the one the migration connects with. No deadline anywhere: a failed init container is retried by the
    kubelet until it succeeds."""

    def test_openfga_db_app_waits_on_keycloak_s_namespace(self):
        self.kube.forbidden.add(("get", "keycloak", "inkcast-staging-realm"))
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("deploy/kustomize/shared/keycloak-access", err)
        self.assertNotIn(("inkcast-staging", "openfga-db-app"), self.kube.objects, "the migration's input is not made")

    @unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
    def test_migrate_runs_after_env_secrets_in_the_same_pod_and_no_job_remains(self):
        for target in ("environments/staging", "environments/production", "overlays/ci-env-secrets"):
            with self.subTest(target=target):
                docs = render(target)
                pod = pod_spec(one(docs, "Deployment", "openfga"))
                self.assertEqual([c["name"] for c in pod["initContainers"]], ["env-secrets", "migrate"])
                migrate = pod["initContainers"][1]
                uri = {e["name"]: e for e in migrate["env"]}["OPENFGA_DATASTORE_URI"]
                self.assertEqual(uri["valueFrom"]["secretKeyRef"], {"name": "openfga-db-app", "key": "uri"})
                self.assertNotIn("restartPolicy", migrate, "run to completion before the server, not a sidecar")
                self.assertFalse([d for d in docs if d["kind"] == "Job"], "no Job: nothing with a deadline, nothing immutable")


ROOT = pathlib.Path(__file__).resolve().parents[1]
SA = "inkcast-env-secrets"
ACCOUNTS = {f"system:serviceaccount:inkcast-{side}:{SA}" for side in ("staging", "prod")}


class AdmissionDenials(EnvSecretsCase):
    """C7: a 403 from an admission policy or webhook is not a missing Role; telling the operator to apply the Role
    sends them to the wrong place. An authorization 403 keeps the Role's location (Failures in test_env_secrets)."""

    def test_an_admission_denial_names_the_policy_not_a_role(self):
        for message, names in (
                ("secrets \"inkcast-pg-openfga-staging\" is forbidden: ValidatingAdmissionPolicy "
                 "'inkcast-env-secrets-creates-its-own' with binding 'inkcast-env-secrets-creates-its-own' denied "
                 "request: inkcast env-secrets of staging creates only its own Secrets, not inkcast-db/x",
                 "deploy/kustomize/shared/env-secrets-admission"),
                ('admission webhook "secrets.policy.example" denied the request: not here', "admission")):
            with self.subTest(message=message[:40]):
                self.kube = FakeKube()
                self.kube.answers[("create", "inkcast-db", "inkcast-pg-openfga-staging")] = status(403, "Forbidden", message)
                code, _, err = self.run_main()
                self.assertEqual(code, 1)
                self.assertIn("create Secret inkcast-db/inkcast-pg-openfga-staging: HTTP 403 Forbidden", err)
                self.assertNotIn("The Role that allows it", err)
                self.assertIn(names, err)


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
class EachSideCreatesItsOwn(unittest.TestCase):
    """C1: RBAC cannot narrow `create` to a name, so either side's account could create the other's shared Secret
    first, and the other side would adopt that value (its database password, its client secrets). The admission
    policy narrows it; the kind e2e proves it denies (scripts/dev/env-secrets-e2e.sh, step 1b)."""

    def test_the_policy_is_cluster_scoped_bound_to_deny_and_names_each_side_s_four_secrets(self):
        docs = render("shared/env-secrets-admission")
        self.assertEqual(sorted(d["kind"] for d in docs), ["ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"])
        for d in docs:
            self.assertNotIn("namespace", d["metadata"], f"{d['kind']} is cluster-scoped")
        policy = one(docs, "ValidatingAdmissionPolicy", "inkcast-env-secrets-creates-its-own")["spec"]
        binding = one(docs, "ValidatingAdmissionPolicyBinding", "inkcast-env-secrets-creates-its-own")["spec"]
        self.assertEqual((binding["policyName"], binding["validationActions"]), ("inkcast-env-secrets-creates-its-own", ["Deny"]))
        self.assertNotIn("matchResources", binding, "every namespace: the shared ones and both sides'")
        self.assertEqual(policy["failurePolicy"], "Fail")
        self.assertEqual(policy["matchConstraints"]["resourceRules"],
                         [{"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE"], "resources": ["secrets"]}])
        (condition,) = policy["matchConditions"]
        self.assertEqual(set(re.findall(r"'([^']+)'", condition["expression"])), ACCOUNTS, "these two accounts only")
        (rule,) = policy["validations"]
        names = set(re.findall(r"'([^']+)'", rule["expression"]))
        for name in ("inkcast-secrets", "openfga-db-app", "inkcast-db", "inkcast-pg-openfga-", "keycloak", "-realm"):
            self.assertIn(name, names)

    def test_the_policy_is_applied_before_the_roles_that_grant_create(self):
        readme = (ROOT / "README.md").read_text()
        e2e = (ROOT / "scripts/dev/env-secrets-e2e.sh").read_text()
        for text, first, after in ((readme, "oc apply -k deploy/kustomize/shared/env-secrets-admission",
                                    ("oc apply -k deploy/kustomize/shared/inkcast-db",
                                     "oc apply -k deploy/kustomize/shared/keycloak-access")),
                                   (e2e, '$k apply -k "$ROOT/deploy/kustomize/shared/env-secrets-admission"',
                                    ('$k apply -k "$ROOT/deploy/kustomize/overlays/ci-inkcast-db"',
                                     '$k apply -k "$ROOT/deploy/kustomize/shared/keycloak-access"'))):
            self.assertIn(first, text)
            for later in after:
                self.assertLess(text.index(first), text.index(later), later)
        self.assertIn("denied inkcast-db inkcast-pg-openfga-prod", e2e)
        self.assertIn("denied keycloak inkcast-prod-realm", e2e)


def grants(docs: list[dict]) -> dict[tuple, set]:
    """(kind, namespace, name) of each subject -> {(namespace or *, verb, resource, resourceName or *)}."""
    rules = {(d["metadata"].get("namespace") if d["kind"] == "Role" else None, d["metadata"]["name"]): d.get("rules", [])
             for d in docs if d["kind"] in ("Role", "ClusterRole")}
    found: dict[tuple, set] = {}
    for b in (d for d in docs if d["kind"] in ("RoleBinding", "ClusterRoleBinding")):
        ns, ref = b["metadata"].get("namespace"), b["roleRef"]
        bound = rules.get((ns if ref["kind"] == "Role" else None, ref["name"]),
                          [{"verbs": [f"all of {ref['kind']} {ref['name']}"], "resources": ["*"]}])
        for s in b.get("subjects", []):
            who = (s["kind"], s.get("namespace") or ns, s["name"])
            found.setdefault(who, set()).update((ns or "*", v, res, n) for r in bound for v in r["verbs"]
                                                for res in r["resources"] for n in r.get("resourceNames") or ["*"])
    return found


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
class EffectiveRbac(unittest.TestCase):
    """C4: the manifest tests check objects by name; this checks what each account is granted, from every binding the
    lab applies, so a widened rule or an extra binding fails it."""

    LAB = ("environments/staging", "environments/production", "shared/inkcast-db", "shared/keycloak-access")

    def test_each_side_s_account_is_granted_exactly_its_own_secrets_and_nobody_else_is(self):
        found = grants([d for target in self.LAB for d in render(target)])
        for ns, side in (("inkcast-staging", "staging"), ("inkcast-prod", "prod")):
            own = {(ns, "get", "inkcast-secrets"), (ns, "patch", "inkcast-secrets"), (ns, "get", "openfga-db-app"),
                   (ns, "create", "*"), ("inkcast-db", "get", f"inkcast-pg-openfga-{side}"), ("inkcast-db", "create", "*"),
                   ("keycloak", "get", f"inkcast-{side}-realm"), ("keycloak", "patch", f"inkcast-{side}-realm"),
                   ("keycloak", "create", "*")}
            self.assertEqual(found.pop(("ServiceAccount", ns, SA)), {(n, v, "secrets", r) for n, v, r in own}, side)
        self.assertEqual({who: g for who, g in found.items() if any(res in ("secrets", "*") for _, _, res, _ in g)},
                         {}, "no other subject reaches a Secret through what the lab applies")


class PatchRaceRereads(EnvSecretsCase):
    """C5: a patch that loses a race to an unrelated change (another key added) is sent again on the new version;
    the existing race tests pass even when a patch's conflict is ignored."""

    def test_a_patch_that_loses_a_race_to_an_unrelated_change_is_sent_again_on_the_new_version(self):
        self.seed_db_pair("staging", "c3" * 24)
        self.kube.seed("keycloak", "inkcast-staging-realm", **{"identity-client-secret": "I" * 32, "sync-client-secret": "S" * 32})
        self.kube.seed("inkcast-staging", "inkcast-secrets", **{"secret-key": "k" * 64, "oidc-client-secret": "I" * 32,
                       "keycloak-sync-secret": "S" * 32})

        def other_writer(method, ns, name):  # someone adds a key env-secrets does not manage
            obj = self.kube.objects[("inkcast-staging", "inkcast-secrets")]
            obj["data"]["github-client-secret"] = base64.b64encode(b"G" * 40).decode()
            self.kube.version += 1
            obj["metadata"]["resourceVersion"] = str(self.kube.version)
        self.kube.before_write = other_writer
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)
        inkcast = self.kube.values("inkcast-staging", "inkcast-secrets")
        self.assertEqual(len(inkcast.get("fga-api-token", "")), 64, "the key was added on the second round")
        self.assertEqual(inkcast["github-client-secret"], "G" * 40)
        self.assertEqual([w for w in self.kube.writes() if w[0] == "PATCH"], [("PATCH", "inkcast-staging", "inkcast-secrets")] * 2)
