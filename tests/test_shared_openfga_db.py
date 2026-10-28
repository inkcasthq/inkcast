# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The lab's two environments on one shared CloudNativePG cluster (E3.F3.9.S5, issue #296): the shared kustomization
(deploy/kustomize/shared/inkcast-db), the environments that drop the component's Cluster and take their own
namespaces, the overlays that keep it, the pull Secret named both ways in the environments and nowhere else, and the
two deploy scripts against a fake ``oc`` that records every argument: a password or token reaches it on stdin only.

The on-cluster half of the acceptance (the Cluster Ready, both databases owned by their roles, each migrate Job
against its own database, a staging role refused by openfga_prod, a third namespace cut off from 5432, the pull
with ghcr-pull) is measured by the integrator on OpenShift Local."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml
from test_openfga_component import KUSTOMIZE, POSTGRES_IMAGE, one, pod_spec, render

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHARED = KUSTOMIZE / "shared" / "inkcast-db"
CREDENTIALS = ROOT / "scripts" / "deploy" / "openfga-db-credentials.sh"
PULL_SECRET = ROOT / "scripts" / "deploy" / "ghcr-pull-secret.sh"
INKCAST_IMAGE = "ghcr.io/inkcasthq/inkcast"
PULL = [{"name": "ghcr-pull"}]
SIDES = {"staging": "inkcast-staging", "prod": "inkcast-prod"}


def load(path: pathlib.Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def templates(docs: list[dict]) -> dict[tuple[str, str], dict]:
    """Every pod template an overlay renders, by (kind, name)."""
    found = {}
    for d in docs:
        if d["kind"] in ("Deployment", "StatefulSet", "Job"):
            found[(d["kind"], d["metadata"]["name"])] = pod_spec(d)
        elif d["kind"] == "CronJob":
            found[(d["kind"], d["metadata"]["name"])] = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return found


def runs_inkcast(pod: dict) -> bool:
    return any(c["image"].split("@")[0].split(":")[0] == INKCAST_IMAGE for c in pod["containers"] + pod.get("initContainers", []))


class SharedFiles(unittest.TestCase):
    """The shared kustomization's own files, without rendering (always runs)."""

    def test_the_cluster_is_the_component_s_with_two_declared_roles_gated_to_their_databases(self):
        component = yaml.safe_load((KUSTOMIZE / "components" / "openfga" / "cluster.yaml").read_text())
        (cluster,) = load(SHARED / "cluster.yaml")
        self.assertEqual((cluster["apiVersion"], cluster["kind"], cluster["metadata"]["name"]),
                         ("postgresql.cnpg.io/v1", "Cluster", "inkcast-pg"))
        self.assertEqual(cluster["spec"]["imageName"], component["spec"]["imageName"], "the same pinned image")
        self.assertRegex(cluster["spec"]["imageName"], POSTGRES_IMAGE)
        self.assertEqual(cluster["spec"]["instances"], 1)
        self.assertEqual(cluster["spec"]["resources"], component["spec"]["resources"], "one pod's worth, like the component")
        roles = {r["name"]: r for r in cluster["spec"]["managed"]["roles"]}
        self.assertEqual(set(roles), {"openfga_staging", "openfga_prod"})
        for side, role in (("staging", roles["openfga_staging"]), ("prod", roles["openfga_prod"])):
            self.assertEqual((role["ensure"], role["login"]), ("present", True), side)
            self.assertEqual(role["passwordSecret"], {"name": f"inkcast-pg-openfga-{side}"}, side)
            self.assertFalse(role.get("superuser") or role.get("createdb") or role.get("createrole"), side)
        # pg_hba: first match wins (PostgreSQL), after the operator's fixed rules and before its default. Each role
        # over TLS to its own database, then the role rejected for anything else: openfga_prod, or no TLS. The bootstrap
        # role app (unused) is refused every network connection.
        self.assertEqual(cluster["spec"]["postgresql"]["pg_hba"], [
            "hostssl openfga_staging openfga_staging all scram-sha-256",
            "hostssl openfga_prod openfga_prod all scram-sha-256",
            "host all openfga_staging all reject",
            "host all openfga_prod all reject",
            "host all app all reject",
        ])
        self.assertEqual(cluster["spec"]["bootstrap"]["initdb"], {"database": "app", "owner": "app"},
                         "the operator's convention, unused; both OpenFGA databases are Database objects")
        self.assertNotIn("enableSuperuserAccess", cluster["spec"], "the operator's default: no superuser over the network")

    def test_the_databases_are_owned_by_their_roles_and_retained(self):
        databases = {d["metadata"]["name"]: d for d in load(SHARED / "databases.yaml")}
        self.assertEqual(set(databases), {"openfga-staging", "openfga-prod"})
        for name, db in databases.items():
            self.assertEqual((db["apiVersion"], db["kind"]), ("postgresql.cnpg.io/v1", "Database"), name)
            self.assertEqual(db["spec"]["cluster"], {"name": "inkcast-pg"}, name)
            self.assertEqual(db["spec"]["name"], name.replace("-", "_"), name)
            self.assertEqual(db["spec"]["owner"], db["spec"]["name"], f"{name}: owned by its own role")
            self.assertEqual(db["spec"]["databaseReclaimPolicy"], "retain", name)
            self.assertNotIn("ensure", db["spec"], name)

    def test_the_network_policy_admits_openfga_the_operator_and_the_instances_only(self):
        """5432 from OpenFGA's pods (server and migrate Job) in the two app namespaces; 8000 and 5432 from the
        operator (security.md "Network Policies", networking.md, and the operator's sample policy at v1.30.1) and
        from the cluster's own pods; nothing else, so a third namespace cannot open 5432."""
        (policy,) = load(SHARED / "networkpolicy.yaml")
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": {"cnpg.io/cluster": "inkcast-pg"}})
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
        self.assertNotIn("egress", policy["spec"], "the instance manager reports to the Kubernetes API")
        app_rule, operator_rule = policy["spec"]["ingress"]
        self.assertEqual([p["port"] for p in app_rule["ports"]], [5432])
        namespaces = {peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] for peer in app_rule["from"]}
        self.assertEqual(namespaces, set(SIDES.values()))
        component = KUSTOMIZE / "components" / "openfga"
        (deployment,) = load(component / "deployment.yaml")
        (job,) = load(component / "job.yaml")
        for peer in app_rule["from"]:
            wanted = peer["podSelector"]["matchLabels"].items()
            self.assertLessEqual(wanted, deployment["spec"]["template"]["metadata"]["labels"].items(), "the server")
            self.assertLessEqual(wanted, job["spec"]["template"]["metadata"]["labels"].items(), "the migrate Job")
            self.assertIn("app.kubernetes.io/name", peer["podSelector"]["matchLabels"], "not every pod of the namespace")
        self.assertEqual({p["port"] for p in operator_rule["ports"]}, {8000, 5432})
        operator, instances = operator_rule["from"]
        self.assertEqual(operator["namespaceSelector"]["matchLabels"], {"kubernetes.io/metadata.name": "openshift-operators"})
        self.assertEqual(operator["podSelector"]["matchLabels"], {"app.kubernetes.io/name": "cloudnative-pg"},
                         "the operator Deployment's label in releases/cnpg-1.30.1.yaml")
        self.assertEqual(instances, {"podSelector": {"matchLabels": {"cnpg.io/cluster": "inkcast-pg"}}})

    def test_the_shared_image_is_pinned_and_listed(self):
        """scripts/images.py (CI's `verify`) sees the shared Cluster's image as it sees the component's."""
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "images.py"), "list"], capture_output=True, text=True,
                             check=True, cwd=ROOT).stdout
        mine = [line for line in out.splitlines() if "shared/inkcast-db/" in line]
        self.assertEqual(len(mine), 1, out)
        self.assertNotIn("not pinned", mine[0])
        self.assertIn("postgresql:", mine[0])

    def test_ci_renders_the_shared_kustomization_with_the_overlays(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("deploy/kustomize/shared/*", ci)

    def test_the_restore_job_runs_as_the_wired_serviceaccount(self):
        """The ad hoc restore Job (scripts/ops/restore.sh) is not in the environments' manifests: the ServiceAccount
        it names is what carries the pull Secret to it."""
        self.assertIn("serviceAccountName: inkcast", (ROOT / "scripts" / "ops" / "restore.sh").read_text())


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
class Rendered(unittest.TestCase):
    ENVIRONMENTS = {"environments/staging": "inkcast-staging", "environments/production": "inkcast-prod"}
    OVERLAYS = {"overlays/kubernetes": "inkcast", "overlays/openshift": "inkcast", "overlays/ci-openfga": "inkcast-openfga"}

    @classmethod
    def setUpClass(cls):
        cls.docs = {name: render(name) for name in ("shared/inkcast-db", *cls.ENVIRONMENTS, *cls.OVERLAYS)}

    def test_the_shared_kustomization_renders_the_namespace_the_cluster_both_databases_and_the_policy(self):
        docs = self.docs["shared/inkcast-db"]
        self.assertEqual({(d["kind"], d["metadata"]["name"]) for d in docs},
                         {("Namespace", "inkcast-db"), ("Cluster", "inkcast-pg"), ("Database", "openfga-staging"),
                          ("Database", "openfga-prod"), ("NetworkPolicy", "inkcast-pg")})
        for d in docs:
            if d["kind"] != "Namespace":
                self.assertEqual(d["metadata"]["namespace"], "inkcast-db", d["kind"])
        labels = one(docs, "Namespace", "inkcast-db")["metadata"]["labels"]
        self.assertEqual(labels["pod-security.kubernetes.io/enforce"], "restricted")
        cluster = one(docs, "Cluster", "inkcast-pg")
        self.assertEqual({r["name"] for r in cluster["spec"]["managed"]["roles"]}, {"openfga_staging", "openfga_prod"})

    def test_the_environments_drop_the_cluster_take_their_namespaces_and_keep_openfga_on_the_app_secret(self):
        for name, namespace in self.ENVIRONMENTS.items():
            with self.subTest(environment=name):
                docs = self.docs[name]
                self.assertFalse([d for d in docs if d["kind"] == "Cluster"], "the shared cluster serves both")
                self.assertEqual({d["metadata"].get("namespace") for d in docs if d["kind"] != "Namespace"}, {namespace})
                for obj in (one(docs, "Deployment", "openfga"), one(docs, "Job", "openfga-migrate")):
                    env = {e["name"]: e for e in pod_spec(obj)["containers"][0]["env"]}
                    self.assertEqual(env["OPENFGA_DATASTORE_URI"]["valueFrom"],
                                     {"secretKeyRef": {"name": "openfga-db-app", "key": "uri"}}, obj["kind"])

    def test_the_overlays_keep_the_component_s_cluster(self):
        for name, namespace in self.OVERLAYS.items():
            with self.subTest(overlay=name):
                cluster = one(self.docs[name], "Cluster", "openfga-db")
                self.assertEqual(cluster["metadata"]["namespace"], namespace)
                self.assertEqual(cluster["spec"]["bootstrap"]["initdb"], {"database": "openfga", "owner": "openfga"})

    def test_the_environments_name_the_pull_secret_both_ways_and_the_overlays_not_at_all(self):
        """Every pod that runs the Inkcast image: its ServiceAccount carries ghcr-pull (what `oc secrets link
        --for=pull` writes) and so does its pod spec; the pods that run other images carry nothing. The inventory
        of such workloads is pinned, so a new one that runs the image is noticed here."""
        for name in self.ENVIRONMENTS:
            with self.subTest(environment=name):
                docs = self.docs[name]
                self.assertEqual(one(docs, "ServiceAccount", "inkcast").get("imagePullSecrets"), PULL)
                inkcast = {key: pod for key, pod in templates(docs).items() if runs_inkcast(pod)}
                # The model writer (E3.F3.9.S6) is an init container in the Deployment's pod: the same pull secret.
                self.assertEqual(set(inkcast), {("Deployment", "inkcast"), ("CronJob", "inkcast-backup")})
                for key, pod in templates(docs).items():
                    if key in inkcast:
                        self.assertEqual(pod.get("serviceAccountName"), "inkcast", key)
                        self.assertEqual(pod.get("imagePullSecrets"), PULL, key)
                    else:
                        self.assertNotIn("imagePullSecrets", pod, f"{key}: a public image")
        for name in self.OVERLAYS:
            with self.subTest(overlay=name):
                docs = self.docs[name]
                self.assertNotIn("imagePullSecrets", one(docs, "ServiceAccount", "inkcast"))
                for key, pod in templates(docs).items():
                    self.assertNotIn("imagePullSecrets", pod, key)
                self.assertNotIn("ghcr-pull", yaml.safe_dump_all(docs))


_FAKE_OC = r'''
"""A fake `oc` for the deploy scripts: a namespace is a directory under $STATE, a Secret a JSON file in it (type and
base64 data, as `oc get -o json` shows them), and every call is one line of $CALLS, arguments only."""
import base64
import json
import os
import pathlib
import sys

import yaml

state = pathlib.Path(os.environ["STATE"])
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as calls:
    calls.write(" ".join(args) + "\n")
if os.environ.get("FAKE_OC_DOWN"):
    print("The connection to the server api.crc.testing:6443 was refused - did you specify the right host or port?", file=sys.stderr)
    sys.exit(1)
ns = None
if args[:1] == ["-n"]:
    ns, args = args[1], args[2:]
verb = args[0]


def not_found(what: str) -> None:
    print(f"Error from server (NotFound): {what} not found", file=sys.stderr)
    sys.exit(1)


if verb == "get" and args[1] == "namespace":
    if (state / args[2]).is_dir():
        print(f"namespace/{args[2]}")
        sys.exit(0)
    not_found(f'namespaces "{args[2]}"')
if verb == "get" and args[1] == "secret":
    name = args[2]
    path = state / ns / f"{name}.json"
    if not path.exists():
        sys.exit(0) if "--ignore-not-found" in args else not_found(f'secrets "{name}"')
    obj = json.loads(path.read_text())
    out = args[args.index("-o") + 1]
    if out == "name":
        print(f"secret/{name}")
    elif out.startswith("jsonpath={.data.") and out.endswith("}"):
        sys.stdout.write(obj["data"].get(out[len("jsonpath={.data."):-1], ""))
    else:
        print(f"unsupported output {out}", file=sys.stderr)
        sys.exit(3)
    sys.exit(0)
if verb in ("create", "replace") and args[1:] == ["-f", "-"]:
    obj = yaml.safe_load(sys.stdin.read())
    name = obj["metadata"]["name"]
    if not (state / ns).is_dir():
        not_found(f'namespaces "{ns}"')
    path = state / ns / f"{name}.json"
    if verb == "create" and path.exists():
        print(f'Error from server (AlreadyExists): secrets "{name}" already exists', file=sys.stderr)
        sys.exit(1)
    if verb == "replace" and not path.exists():
        not_found(f'secrets "{name}"')
    data = {k: base64.b64encode(str(v).encode()).decode() for k, v in obj.get("stringData", {}).items()}
    path.write_text(json.dumps({"kind": "Secret", "metadata": obj["metadata"], "type": obj.get("type", "Opaque"), "data": data}))
    print(f"secret/{name} {'created' if verb == 'create' else 'replaced'}")
    sys.exit(0)
print(f"unsupported: {args}", file=sys.stderr)
sys.exit(3)
'''


class FakeOcCase(unittest.TestCase):
    """A fake ``oc`` first on PATH, with the three namespaces of the lab; a test removes what its case lacks."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="deploy-script-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        fake = self.dir / "bin" / "oc"
        fake.parent.mkdir()
        fake.write_text(f"#!{sys.executable}\n{_FAKE_OC}")
        fake.chmod(0o755)
        self.state = self.dir / "state"
        for namespace in ("inkcast-db", *SIDES.values()):
            (self.state / namespace).mkdir(parents=True)
        self.calls_file = self.dir / "calls"
        self.calls_file.write_text("")
        self.env = {**os.environ, "PATH": f"{fake.parent}:{os.environ.get('PATH', '')}", "STATE": str(self.state),
                    "CALLS": str(self.calls_file)}

    def run_script(self, script: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
        self.calls_file.write_text("")
        return subprocess.run(["bash", str(script), *args], env=self.env, capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        return self.calls_file.read_text().splitlines()

    def stored(self, namespace: str, name: str) -> dict | None:
        path = self.state / namespace / f"{name}.json"
        if not path.exists():
            return None
        obj = json.loads(path.read_text())
        obj["decoded"] = {k: base64.b64decode(v).decode() for k, v in obj["data"].items()}
        return obj

    def seed(self, namespace: str, name: str, kind: str, **values: str) -> None:
        data = {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}
        (self.state / namespace / f"{name}.json").write_text(
            json.dumps({"kind": "Secret", "metadata": {"name": name}, "type": kind, "data": data}))

    def assert_never_seen(self, value: str, done: subprocess.CompletedProcess) -> None:
        self.assertNotIn(value, done.stdout)
        self.assertNotIn(value, done.stderr)
        self.assertNotIn(value, self.calls_file.read_text(), "in an argument of oc")


def uri(side: str, pw: str) -> str:
    return f"postgres://openfga_{side}:{pw}@inkcast-pg-rw.inkcast-db.svc:5432/openfga_{side}?sslmode=require"


class CredentialsScript(FakeOcCase):
    """scripts/deploy/openfga-db-credentials.sh: one password per side, on stdin only, written once."""

    def test_a_fresh_pair_is_created_with_the_password_on_stdin_only(self):
        for side, namespace in SIDES.items():
            with self.subTest(side=side):
                done = self.run_script(CREDENTIALS, side)
                self.assertEqual(done.returncode, 0, done.stderr)
                role = self.stored("inkcast-db", f"inkcast-pg-openfga-{side}")
                self.assertEqual(role["type"], "kubernetes.io/basic-auth")
                self.assertEqual(role["metadata"]["labels"]["cnpg.io/reload"], "true")
                self.assertEqual(set(role["decoded"]), {"username", "password"})
                self.assertEqual(role["decoded"]["username"], f"openfga_{side}")
                pw = role["decoded"]["password"]
                self.assertRegex(pw, r"^[0-9a-f]{48}$", "hex: URL-safe, no percent-encoding")
                app = self.stored(namespace, "openfga-db-app")
                self.assertEqual(app["decoded"], {"username": f"openfga_{side}", "password": pw, "uri": uri(side, pw)})
                self.assert_never_seen(pw, done)
                creates = [c for c in self.calls() if c.endswith("create -f -")]
                self.assertEqual(creates, ["-n inkcast-db create -f -", f"-n {namespace} create -f -"],
                                 "the role's Secret first, then the app's")
                for named in (f"inkcast-db/inkcast-pg-openfga-{side}", f"{namespace}/openfga-db-app", "sslmode=require"):
                    self.assertIn(named, done.stdout)

    def test_a_second_run_keeps_the_pair_and_writes_nothing(self):
        first = self.run_script(CREDENTIALS, "staging")
        self.assertEqual(first.returncode, 0, first.stderr)
        before = (self.stored("inkcast-db", "inkcast-pg-openfga-staging"), self.stored("inkcast-staging", "openfga-db-app"))
        again = self.run_script(CREDENTIALS, "staging")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Kept", again.stdout)
        self.assertFalse([c for c in self.calls() if "create" in c or "replace" in c or "delete" in c], self.calls())
        self.assertEqual((self.stored("inkcast-db", "inkcast-pg-openfga-staging"), self.stored("inkcast-staging", "openfga-db-app")), before)
        self.assert_never_seen(before[0]["decoded"]["password"], again)

    def test_half_a_pair_fails_naming_both_secrets_and_changes_nothing(self):
        pw = "a" * 48
        self.seed("inkcast-db", "inkcast-pg-openfga-prod", "kubernetes.io/basic-auth", username="openfga_prod", password=pw)
        done = self.run_script(CREDENTIALS, "prod")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("inkcast-db/inkcast-pg-openfga-prod exists but inkcast-prod/openfga-db-app does not", done.stderr)
        self.assertIsNone(self.stored("inkcast-prod", "openfga-db-app"))
        self.assertFalse([c for c in self.calls() if "create" in c], self.calls())
        self.assert_never_seen(pw, done)
        # The other half.
        (self.state / "inkcast-db" / "inkcast-pg-openfga-prod.json").unlink()
        self.seed("inkcast-prod", "openfga-db-app", "Opaque", username="openfga_prod", password=pw, uri=uri("prod", pw))
        done = self.run_script(CREDENTIALS, "prod")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("inkcast-prod/openfga-db-app exists but inkcast-db/inkcast-pg-openfga-prod does not", done.stderr)
        self.assertIsNone(self.stored("inkcast-db", "inkcast-pg-openfga-prod"))
        self.assertFalse([c for c in self.calls() if "create" in c], self.calls())
        self.assert_never_seen(pw, done)

    def test_a_pair_that_disagrees_fails_naming_the_secrets_and_changes_nothing(self):
        one_pw, other_pw = "b" * 48, "c" * 48
        cases = {
            "a different password": dict(username="openfga_staging", password=other_pw, uri=uri("staging", other_pw)),
            "the uri without TLS": dict(username="openfga_staging", password=one_pw,
                                        uri=uri("staging", one_pw).replace("sslmode=require", "sslmode=disable")),
            "the uri at the wrong database": dict(username="openfga_staging", password=one_pw,
                                                  uri=uri("staging", one_pw).replace("/openfga_staging?", "/openfga_prod?")),
            "the uri's password unlike the key": dict(username="openfga_staging", password=one_pw, uri=uri("staging", other_pw)),
        }
        for what, app in cases.items():
            with self.subTest(what=what):
                self.seed("inkcast-db", "inkcast-pg-openfga-staging", "kubernetes.io/basic-auth", username="openfga_staging", password=one_pw)
                self.seed("inkcast-staging", "openfga-db-app", "Opaque", **app)
                done = self.run_script(CREDENTIALS, "staging")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("inkcast-staging/openfga-db-app disagrees with inkcast-db/inkcast-pg-openfga-staging", done.stderr)
                self.assertFalse([c for c in self.calls() if "create" in c or "replace" in c], self.calls())
                self.assertEqual(self.stored("inkcast-staging", "openfga-db-app")["decoded"], app, "nothing changed")
                for pw in (one_pw, other_pw):
                    self.assert_never_seen(pw, done)

    def test_a_role_secret_that_is_not_the_role_s_is_named(self):
        pw = "d" * 48
        self.seed("inkcast-db", "inkcast-pg-openfga-staging", "kubernetes.io/basic-auth", username="someone_else", password=pw)
        self.seed("inkcast-staging", "openfga-db-app", "Opaque", username="openfga_staging", password=pw, uri=uri("staging", pw))
        done = self.run_script(CREDENTIALS, "staging")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("inkcast-db/inkcast-pg-openfga-staging is not a basic-auth Secret for role openfga_staging", done.stderr)
        self.assertFalse([c for c in self.calls() if "create" in c], self.calls())
        self.assert_never_seen(pw, done)

    def test_a_missing_namespace_fails_before_anything_is_written(self):
        shutil.rmtree(self.state / "inkcast-staging")
        done = self.run_script(CREDENTIALS, "staging")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("Namespace inkcast-staging does not exist: oc new-project inkcast-staging", done.stderr)
        self.assertIsNone(self.stored("inkcast-db", "inkcast-pg-openfga-staging"), "no half pair")
        self.assertFalse([c for c in self.calls() if "create" in c], self.calls())
        shutil.rmtree(self.state / "inkcast-db")
        done = self.run_script(CREDENTIALS, "prod")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("Namespace inkcast-db does not exist: oc apply -k deploy/kustomize/shared/inkcast-db", done.stderr)

    def test_a_side_that_is_not_staging_or_prod_is_refused_before_oc(self):
        for bad in ((), ("dev",), ("production",)):
            with self.subTest(args=bad):
                done = self.run_script(CREDENTIALS, *bad)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("usage", done.stderr)
                self.assertEqual(self.calls(), [])

    def test_an_unreachable_cluster_is_oc_s_own_error_not_a_missing_namespace(self):
        self.env["FAKE_OC_DOWN"] = "1"
        done = self.run_script(CREDENTIALS, "staging")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("connection to the server", done.stderr)
        self.assertNotIn("does not exist", done.stderr)
        self.assertFalse([c for c in self.calls() if "create" in c], self.calls())


class PullSecretScript(FakeOcCase):
    """scripts/deploy/ghcr-pull-secret.sh: the token from a file to oc's stdin, the Secret a docker config for ghcr.io."""

    def pat_file(self, text: str) -> pathlib.Path:
        path = self.dir / "pat"
        path.write_text(text)
        return path

    def test_the_token_goes_from_the_file_to_stdin_and_the_secret_is_a_docker_config(self):
        pat = "ghp_" + "A1b2" * 9
        done = self.run_script(PULL_SECRET, "staging", "octocat", str(self.pat_file(pat + "\n")))
        self.assertEqual(done.returncode, 0, done.stderr)
        stored = self.stored("inkcast-staging", "ghcr-pull")
        self.assertEqual(stored["type"], "kubernetes.io/dockerconfigjson")
        self.assertEqual(set(stored["decoded"]), {".dockerconfigjson"})
        config = json.loads(stored["decoded"][".dockerconfigjson"])
        self.assertEqual(config, {"auths": {"ghcr.io": {"username": "octocat", "password": pat,
                                                        "auth": base64.b64encode(f"octocat:{pat}".encode()).decode()}}})
        self.assert_never_seen(pat, done)
        self.assertEqual([c for c in self.calls() if c.endswith("-f -")], ["-n inkcast-staging create -f -"])
        self.assertIn("Created Secret inkcast-staging/ghcr-pull", done.stdout)
        self.assertIsNone(self.stored("inkcast-prod", "ghcr-pull"), "one side at a time")

    def test_a_second_run_replaces_the_secret_so_a_token_is_rotated(self):
        first, second = "ghp_" + "x" * 36, "ghp_" + "y" * 36
        self.assertEqual(self.run_script(PULL_SECRET, "prod", "octocat", str(self.pat_file(first))).returncode, 0)
        done = self.run_script(PULL_SECRET, "prod", "octocat", str(self.pat_file(second)))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Replaced Secret inkcast-prod/ghcr-pull", done.stdout)
        self.assertEqual([c for c in self.calls() if c.endswith("-f -")], ["-n inkcast-prod replace -f -"])
        config = json.loads(self.stored("inkcast-prod", "ghcr-pull")["decoded"][".dockerconfigjson"])
        self.assertEqual(config["auths"]["ghcr.io"]["password"], second)
        for pat in (first, second):
            self.assert_never_seen(pat, done)

    def test_a_file_that_is_not_one_token_is_refused_before_oc(self):
        for text in ("ghp_one\nghp_two\n", "ghp_with space\n", "", "ghp_quote\"\n", "ghp_x\r\n"):
            with self.subTest(text=text):
                done = self.run_script(PULL_SECRET, "staging", "octocat", str(self.pat_file(text)))
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("must hold one token", done.stderr)
                self.assertEqual(self.calls(), [])
                self.assertNotIn("ghp_", done.stderr, "the file's content is not echoed")
        done = self.run_script(PULL_SECRET, "staging", "octocat", str(self.dir / "missing"))
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_bad_side_or_username_is_refused_before_oc(self):
        path = str(self.pat_file("ghp_" + "z" * 36))
        for args in (("dev", "octocat", path), ("staging",), ("staging", "octocat"), ("staging", "bad name", path),
                     ("staging", "-n", path)):
            with self.subTest(args=args):
                done = self.run_script(PULL_SECRET, *args)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertEqual(self.calls(), [])

    def test_a_missing_namespace_fails_naming_it(self):
        shutil.rmtree(self.state / "inkcast-prod")
        done = self.run_script(PULL_SECRET, "prod", "octocat", str(self.pat_file("ghp_" + "w" * 36)))
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("Namespace inkcast-prod does not exist: oc new-project inkcast-prod", done.stderr)
        self.assertFalse([c for c in self.calls() if c.endswith("-f -")], self.calls())

    def test_an_unreachable_cluster_is_oc_s_own_error_not_a_missing_namespace(self):
        self.env["FAKE_OC_DOWN"] = "1"
        pat = "ghp_" + "v" * 36
        done = self.run_script(PULL_SECRET, "staging", "octocat", str(self.pat_file(pat)))
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("connection to the server", done.stderr)
        self.assertNotIn("does not exist", done.stderr)
        self.assertFalse([c for c in self.calls() if c.endswith("-f -")], self.calls())
        self.assert_never_seen(pat, done)


if __name__ == "__main__":
    unittest.main()
