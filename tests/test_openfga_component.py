# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OpenFGA deployed with Inkcast (E3.F3.9.S1, issue #279): the Kustomize component and the overlays that use it,
the pod spec under Pod Security "restricted" and the restricted-v2 SCC, the images pinned by digest, the store and
model ids pinned per environment (and the refusal, naming them, until they are), and ``inkcast authz write-model``
against the fake OpenFGA. The kind job (scripts/dev/openfga-e2e.sh) proves the same on a cluster."""

from __future__ import annotations

import copy
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import httpx2
import yaml
from fakes import Fakes
from test_authz import free_port, serve

from inkcast.authz_cli import authz_main
from inkcast.libs.authz.model import PACKAGED_MODEL
from inkcast.libs.authz.openfga import same_model
from inkcast.libs.config import ConfigError, load_config

ROOT = pathlib.Path(__file__).resolve().parents[1]
KUSTOMIZE = ROOT / "deploy" / "kustomize"
COMPONENT = KUSTOMIZE / "components" / "openfga"
OPENFGA_IMAGE = re.compile(r"^openfga/openfga:v1\.22\.0@sha256:[0-9a-f]{64}$")
POSTGRES_IMAGE = re.compile(r"^ghcr\.io/cloudnative-pg/postgresql:[0-9][A-Za-z0-9._-]*@sha256:[0-9a-f]{64}$")


def render(path: str) -> list[dict]:
    """The objects `kubectl kustomize` renders for an overlay or environment (CI's manifests job renders the same)."""
    out = subprocess.run(["kubectl", "kustomize", str(KUSTOMIZE / path)], capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def one(docs: list[dict], kind: str, name: str) -> dict:
    (found,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return found


def pod_spec(obj: dict) -> dict:
    return obj["spec"]["template"]["spec"]


def assert_restricted_v2(case: unittest.TestCase, pod: dict, what: str) -> None:
    """What the restricted-v2 SCC and Pod Security "restricted" admit, and nothing that only one of them would:
    no uid, gid or fsGroup (the SCC assigns them; the image's numeric USER satisfies runAsNonRoot elsewhere)."""
    case.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}, what)
    case.assertFalse(pod.get("volumes"), f"{what}: the binary writes nothing, so no volume and no fsGroup")
    case.assertFalse(pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"), what)
    case.assertIs(pod["automountServiceAccountToken"], False, what)
    for container in pod["containers"] + pod.get("initContainers", []):
        sc = container["securityContext"]
        case.assertEqual(sc, {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                              "capabilities": {"drop": ["ALL"]}}, f"{what}: {container['name']}")
        case.assertRegex(container["image"], OPENFGA_IMAGE, f"{what}: {container['name']}")


class ComponentFiles(unittest.TestCase):
    """The component's own files, without rendering (always runs)."""

    def load(self, name: str) -> list[dict]:
        return [d for d in yaml.safe_load_all((COMPONENT / name).read_text()) if d]

    def test_the_server_and_the_migrate_job_fit_restricted_v2(self):
        (deployment,) = self.load("deployment.yaml")
        assert_restricted_v2(self, pod_spec(deployment), "Deployment openfga")
        (job,) = self.load("job.yaml")
        assert_restricted_v2(self, pod_spec(job), "Job openfga-migrate")
        self.assertEqual(pod_spec(job)["restartPolicy"], "OnFailure")
        self.assertEqual(pod_spec(job)["containers"][0]["args"], ["migrate"])
        self.assertEqual(pod_spec(deployment)["containers"][0]["args"], ["run"])

    def test_openfga_reads_its_settings_by_the_server_s_own_names(self):
        """cmd/run/flags.go and cmd/migrate/flags.go at v1.22.0: the datastore from the operator's Secret, the
        preshared key from the Secret Inkcast reads, nothing as a literal."""
        (deployment,) = self.load("deployment.yaml")
        (job,) = self.load("job.yaml")
        for obj, what in ((deployment, "server"), (job, "migrate")):
            env = {e["name"]: e for e in pod_spec(obj)["containers"][0]["env"]}
            self.assertEqual(env["OPENFGA_DATASTORE_ENGINE"]["value"], "postgres", what)
            self.assertEqual(env["OPENFGA_DATASTORE_URI"]["valueFrom"], {"secretKeyRef": {"name": "openfga-db-app", "key": "uri"}}, what)
            for name, e in env.items():
                self.assertNotIn("KEY", {k for k in e if k == "value" and "KEY" in name}, f"{what}: {name} as a literal")
        server = {e["name"]: e for e in pod_spec(deployment)["containers"][0]["env"]}
        self.assertEqual(server["OPENFGA_AUTHN_METHOD"]["value"], "preshared")
        self.assertEqual(server["OPENFGA_AUTHN_PRESHARED_KEYS"]["valueFrom"],
                         {"secretKeyRef": {"name": "inkcast-secrets", "key": "fga-api-token"}})
        ports = {p["name"]: p["containerPort"] for p in pod_spec(deployment)["containers"][0]["ports"]}
        self.assertEqual(ports, {"http": 8080, "grpc": 8081, "metrics": 2112}, "DefaultConfig at v1.22.0")
        for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
            self.assertEqual(pod_spec(deployment)["containers"][0][probe]["httpGet"], {"path": "/healthz", "port": "http"})

    def test_the_database_is_a_cloudnativepg_cluster_with_a_pinned_image(self):
        (cluster,) = self.load("cluster.yaml")
        self.assertEqual((cluster["apiVersion"], cluster["kind"], cluster["metadata"]["name"]),
                         ("postgresql.cnpg.io/v1", "Cluster", "openfga-db"))
        self.assertRegex(cluster["spec"]["imageName"], POSTGRES_IMAGE)
        self.assertEqual(cluster["spec"]["bootstrap"]["initdb"], {"database": "openfga", "owner": "openfga"})
        self.assertEqual(cluster["spec"]["instances"], 1, "production patches the count")

    def test_every_image_the_component_runs_is_pinned_and_listed(self):
        """scripts/images.py (CI's `verify`) sees each one, including the Cluster's imageName."""
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "images.py"), "list"], capture_output=True, text=True,
                             check=True, cwd=ROOT).stdout
        mine = [line for line in out.splitlines() if "components/openfga/" in line]
        self.assertEqual(len(mine), 3, out)
        self.assertFalse([line for line in mine if "not pinned" in line], mine)
        self.assertTrue(any("cluster.yaml" in line and "postgresql:" in line for line in mine), mine)

    def test_inkcast_reaches_openfga_and_reads_the_same_key(self):
        k = yaml.safe_load((COMPONENT / "kustomization.yaml").read_text())
        (gen,) = k["configMapGenerator"]
        self.assertEqual((gen["name"], gen["behavior"]), ("inkcast-config", "merge"))
        self.assertEqual(set(gen["literals"]), {"INKCAST_AUTHZ_ENGINE=openfga", "INKCAST_FGA_API_URL=http://openfga:8080",
                                                "INKCAST_FGA_API_TOKEN_FILE=/var/run/secrets/inkcast/fga-api-token"})
        base = yaml.safe_load((KUSTOMIZE / "base" / "deployment.yaml").read_text())
        mounts = {m["name"]: m["mountPath"] for m in pod_spec(base)["containers"][0]["volumeMounts"]}
        volumes = {v["name"]: v for v in pod_spec(base)["volumes"]}
        self.assertEqual(mounts["secrets"], "/var/run/secrets/inkcast")
        self.assertEqual(volumes["secrets"]["secret"]["secretName"], "inkcast-secrets")
        (policy,) = self.load("networkpolicy.yaml")
        self.assertEqual(policy["spec"]["podSelector"]["matchLabels"], {"app.kubernetes.io/component": "server"})
        (rule,) = policy["spec"]["egress"]
        self.assertEqual([p["port"] for p in rule["ports"]], [8080])
        (deployment,) = self.load("deployment.yaml")
        self.assertLessEqual(rule["to"][0]["podSelector"]["matchLabels"].items(),
                             deployment["spec"]["template"]["metadata"]["labels"].items())

    def test_metrics_go_through_the_existing_servicemonitor(self):
        services = {s["metadata"]["name"]: s for s in self.load("service.yaml")}
        monitor = yaml.safe_load((KUSTOMIZE / "components" / "servicemonitor" / "servicemonitor.yaml").read_text())
        self.assertLessEqual(monitor["spec"]["selector"]["matchLabels"].items(),
                             services["openfga-metrics"]["metadata"]["labels"].items())
        (endpoint,) = monitor["spec"]["endpoints"]
        self.assertEqual([p["name"] for p in services["openfga-metrics"]["spec"]["ports"]], [endpoint["port"]])
        (deployment,) = self.load("deployment.yaml")
        self.assertEqual(deployment["spec"]["template"]["metadata"]["labels"]["inkcast.io/service"], "openfga")


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the overlays (CI's manifests job has it)")
class Rendered(unittest.TestCase):
    """The rendered objects of every overlay and environment that ships the component."""

    @classmethod
    def setUpClass(cls):
        cls.docs = {name: render(name) for name in ("overlays/kubernetes", "overlays/openshift", "overlays/ci-openfga",
                                                     "environments/staging", "environments/production")}

    def config(self, name: str) -> dict[str, str]:
        return one(self.docs[name], "ConfigMap", "inkcast-config")["data"]

    def test_the_self_host_overlays_use_openfga_and_drop_the_local_opt_in(self):
        for name in ("overlays/kubernetes", "overlays/openshift"):
            with self.subTest(overlay=name):
                data = self.config(name)
                self.assertEqual(data["INKCAST_AUTHZ_ENGINE"], "openfga")
                self.assertNotIn("INKCAST_AUTHZ_ALLOW_LOCAL", data)
                self.assertEqual(data["INKCAST_FGA_API_URL"], "http://openfga:8080")
                self.assertEqual(data["INKCAST_FGA_API_TOKEN_FILE"], "/var/run/secrets/inkcast/fga-api-token")
                kinds = {(d["kind"], d["metadata"]["name"]) for d in self.docs[name]}
                for wanted in (("Cluster", "openfga-db"), ("Job", "openfga-migrate"), ("Deployment", "openfga"),
                               ("Service", "openfga"), ("Service", "openfga-metrics"), ("NetworkPolicy", "inkcast-to-openfga")):
                    self.assertIn(wanted, kinds)
                self.assertEqual(one(self.docs[name], "Deployment", "openfga")["metadata"]["namespace"], "inkcast")

    def test_the_rendered_openfga_pods_are_admitted_under_restricted_v2(self):
        """The overlays patch Inkcast's own pod with a uid on Kubernetes; OpenFGA's pod needs none anywhere."""
        for name in self.docs:
            with self.subTest(overlay=name):
                assert_restricted_v2(self, pod_spec(one(self.docs[name], "Deployment", "openfga")), f"{name}: Deployment")
                assert_restricted_v2(self, pod_spec(one(self.docs[name], "Job", "openfga-migrate")), f"{name}: Job")

    def test_the_hosted_environments_pin_the_ids_and_refuse_to_start_until_they_are_real(self):
        """The rendered ConfigMap, loaded as the pod would load it (the key as the mounted file the ConfigMap names):
        the error names the unpinned variables, and once both are set the authorization settings pass."""
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as f:
            f.write("t" * 32)
        self.addCleanup(pathlib.Path(f.name).unlink)
        for name in ("environments/staging", "environments/production"):
            with self.subTest(environment=name):
                data = self.config(name)
                self.assertEqual((data["INKCAST_AUTHZ_ENGINE"], data["INKCAST_AUTHZ_ALLOW_LOCAL"]), ("openfga", "false"))
                self.assertIn("INKCAST_FGA_STORE_ID", data)
                self.assertIn("INKCAST_FGA_MODEL_ID", data)
                self.assertEqual(data["INKCAST_FGA_API_TOKEN_FILE"], "/var/run/secrets/inkcast/fga-api-token")
                env = {**data, "INKCAST_SECRET_KEY": "k" * 48, "OIDC_CLIENT_SECRET": "s" * 32, "INKCAST_FGA_API_TOKEN_FILE": f.name}
                if not (data["INKCAST_FGA_STORE_ID"] and data["INKCAST_FGA_MODEL_ID"]):
                    with self.assertRaises(ConfigError) as caught:
                        load_config(env).validate(("secret_key", "login", "authz"))
                    self.assertRegex(str(caught.exception), r"INKCAST_AUTHZ_ENGINE=openfga needs INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID")
                    self.assertNotIn("INKCAST_FGA_API_URL", str(caught.exception), "the component sets the URL")
                pinned = {**env, "INKCAST_FGA_STORE_ID": "01JSTORE0000000000000000AA", "INKCAST_FGA_MODEL_ID": "01JMODEL0000000000000000AA"}
                try:
                    load_config(pinned).validate(("secret_key", "login", "authz"))
                except ConfigError as exc:
                    self.assertNotRegex(str(exc), r"AUTHZ|FGA", f"{name}: {exc}")


class Settings(unittest.TestCase):
    """Config.validate names each missing OpenFGA setting, so a deployment reads what to pin."""

    BASE = {"INKCAST_SECRET_KEY": "k" * 48, "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 16,
            "INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_AUTHZ_ENGINE": "openfga"}

    def problem(self, **env) -> str:
        with self.assertRaises(ConfigError) as caught:
            load_config({**self.BASE, **env}).validate()
        return str(caught.exception)

    def test_each_missing_variable_is_named(self):
        self.assertIn("needs INKCAST_FGA_API_URL, INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID", self.problem())
        two = self.problem(INKCAST_FGA_API_URL="http://openfga:8080")
        self.assertIn("needs INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID", two)
        self.assertNotIn("INKCAST_FGA_API_URL", two)
        one_ = self.problem(INKCAST_FGA_API_URL="http://openfga:8080", INKCAST_FGA_STORE_ID="s")
        self.assertIn("needs INKCAST_FGA_MODEL_ID (", one_)
        self.assertNotIn("STORE_ID", one_)
        self.assertIn("inkcast authz write-model", one_)


class SameModel(unittest.TestCase):
    """``same_model`` against the shape OpenFGA v1.22.0 answers (observed 2026-10-28 on the real server: every
    protobuf field populated), and against models that differ in substance."""

    def stored(self, model: dict) -> dict:
        """The packaged model as OpenFGA returns it after a write."""
        out = {"id": "01M60FD22R03BHWMP8BZBSFJF4", **copy.deepcopy(model), "conditions": {}}
        for td in out["type_definitions"]:
            td.setdefault("relations", {})
            if "metadata" not in td:
                td["metadata"] = None
                continue
            td["metadata"].setdefault("module", "")
            td["metadata"].setdefault("source_info", None)
            for meta in td["metadata"]["relations"].values():
                meta.setdefault("directly_related_user_types", [])
                meta.setdefault("module", "")
                meta.setdefault("source_info", None)
                for ref in meta["directly_related_user_types"]:
                    ref.setdefault("condition", "")
            for rewrite in td["relations"].values():
                for child in rewrite.get("union", {}).get("child", [rewrite]):
                    child.get("computedUserset", {}).setdefault("object", "")
        return out

    def test_the_populated_answer_is_the_file_and_changes_are_not(self):
        model = json.loads(PACKAGED_MODEL.read_text())
        self.assertTrue(same_model(self.stored(model), model))
        self.assertNotEqual(self.stored(model), model, "the shapes differ; only the substance is compared")
        org = next(i for i, td in enumerate(model["type_definitions"]) if td["type"] == "organization")
        fewer = copy.deepcopy(model)
        del fewer["type_definitions"][org]["relations"]["can_create_workspace"]
        del fewer["type_definitions"][org]["metadata"]["relations"]["can_create_workspace"]
        more = copy.deepcopy(model)
        more["type_definitions"].append({"type": "team"})
        wider = copy.deepcopy(model)
        wider["type_definitions"][org]["metadata"]["relations"]["admin"]["directly_related_user_types"].append(
            {"type": "organization", "relation": "member"})
        for changed in (fewer, more, wider):
            self.assertFalse(same_model(self.stored(model), changed))
            self.assertFalse(same_model(self.stored(changed), model))


class WriteModel(unittest.TestCase):
    """``inkcast authz write-model`` against the fake OpenFGA (tests/fakes.py): the store is created, the model
    written once, a second run changes nothing, and nothing printed carries the key."""

    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.fakes = Fakes(f"http://127.0.0.1:{port}")
        cls.server = serve(cls.fakes.app(), port)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def setUp(self):
        self.fakes.fga_stores, self.fakes.fga_models, self.fakes.fga_calls = {}, {}, []
        self.env = {"INKCAST_FGA_API_URL": f"{self.fakes.base}/fga", "INKCAST_FGA_API_TOKEN": self.fakes.fga_token}

    def run_cli(self, *args: str, **env: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = authz_main(["write-model", *args], environ={**self.env, **env}, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def ids(self, out: str) -> tuple[str, str]:
        lines = dict(line.split("=", 1) for line in out.splitlines())
        self.assertEqual(set(lines), {"INKCAST_FGA_STORE_ID", "INKCAST_FGA_MODEL_ID"}, out)
        return lines["INKCAST_FGA_STORE_ID"], lines["INKCAST_FGA_MODEL_ID"]

    def test_creates_the_store_writes_the_model_once_and_prints_the_ids(self):
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        store_id, model_id = self.ids(out)
        self.assertEqual(self.fakes.fga_stores, {store_id: "inkcast"})
        (written,) = self.fakes.fga_models[store_id]
        self.assertEqual(written["id"], model_id)
        self.assertEqual(written["type_definitions"], json.loads(PACKAGED_MODEL.read_text())["type_definitions"])
        self.assertIn("created", err)
        self.assertIn("model written", err)
        calls = [(m, p.removeprefix("/fga/")) for m, p, _ in self.fakes.fga_calls]
        self.assertEqual(calls, [("GET", "stores"), ("POST", "stores"), ("GET", f"stores/{store_id}/authorization-models"),
                                 ("POST", f"stores/{store_id}/authorization-models")])
        # Second run: the store is found and the newest version is this model, so nothing is written.
        code, out2, err2 = self.run_cli()
        self.assertEqual(code, 0, err2)
        self.assertEqual(self.ids(out2), (store_id, model_id))
        self.assertEqual(len(self.fakes.fga_models[store_id]), 1, "no second version")
        self.assertEqual(self.fakes.fga_stores, {store_id: "inkcast"}, "no second store")
        self.assertIn("found", err2)
        self.assertIn("unchanged", err2)
        for text in (out, err, out2, err2):
            self.assertNotIn(self.fakes.fga_token, text, "the key never leaves the bearer header")

    def test_a_changed_model_is_a_new_version_in_the_same_store(self):
        code, out, _ = self.run_cli()
        store_id, first = self.ids(out)
        changed = json.loads(PACKAGED_MODEL.read_text())
        changed["type_definitions"].append({"type": "team"})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(changed, f)
        self.addCleanup(pathlib.Path(f.name).unlink)
        code, out, err = self.run_cli("--model", f.name)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(out)[0], store_id)
        self.assertNotEqual(self.ids(out)[1], first)
        self.assertEqual([m["id"] for m in self.fakes.fga_models[store_id]], [first, self.ids(out)[1]])
        self.assertIn(f"the newest was {first}", err)
        # Back to the packaged model: newer than the changed one, so a third version; then idempotent again.
        code, out, _ = self.run_cli()
        self.assertEqual(len(self.fakes.fga_models[store_id]), 3)
        self.assertEqual(self.run_cli()[1], out)
        self.assertEqual(len(self.fakes.fga_models[store_id]), 3)

    def test_the_store_name_and_the_key_file_are_honoured(self):
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as f:
            f.write(self.fakes.fga_token + "\n")
        self.addCleanup(pathlib.Path(f.name).unlink)
        code, out, err = self.run_cli("--store", "inkcast-staging", INKCAST_FGA_API_TOKEN="", INKCAST_FGA_API_TOKEN_FILE=f.name)
        self.assertEqual(code, 0, err)
        self.assertEqual(list(self.fakes.fga_stores.values()), ["inkcast-staging"])
        self.assertEqual(self.fakes.fga_calls[0][1], "/fga/stores")

    def test_refusals_name_the_problem_and_print_no_id(self):
        code, out, err = self.run_cli(INKCAST_FGA_API_URL="")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("INKCAST_FGA_API_URL", err)
        code, out, err = self.run_cli(INKCAST_FGA_API_TOKEN="wrong-key")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("credentials", err)
        self.assertNotIn("wrong-key", err)
        self.assertEqual(self.fakes.fga_stores, {}, "nothing created with a bad key")
        code, out, err = self.run_cli("--model", "/nonexistent/model.json")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("can't be loaded", err)
        self.fakes.fga_stores = {"01JFAKE0000000000000000001": "inkcast", "01JFAKE0000000000000000002": "inkcast"}
        self.fakes.fga_models = {k: [] for k in self.fakes.fga_stores}
        code, out, err = self.run_cli()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("2 stores named 'inkcast'", err)
        self.assertEqual(len(self.fakes.fga_stores), 2, "neither chosen, none created")

    def test_an_unreachable_server_fails_closed(self):
        code, out, err = self.run_cli(INKCAST_FGA_API_URL="http://127.0.0.1:9/fga", INKCAST_FGA_TIMEOUT="1")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("unreachable", err)

    def test_the_launcher_dispatches_authz(self):
        """`inkcast authz ...` reaches authz_main with the process environment (here: no INKCAST_FGA_API_URL)."""
        import contextlib
        from inkcast.launcher import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main(["authz", "write-model"])
        self.assertEqual((caught.exception.code, out.getvalue()), (1, ""))
        self.assertIn("INKCAST_FGA_API_URL is not set", err.getvalue())


class KindJob(unittest.TestCase):
    """The kind CI job applies the component through scripts/dev/openfga-e2e.sh, with CloudNativePG pinned like the
    other cluster tools (scripts/versions.env, a SHA-256 the script checks before applying)."""

    def test_cnpg_is_pinned_and_the_kind_job_runs_the_script(self):
        versions = dict(re.findall(r"^([A-Za-z0-9_]+)=(.*)$", (ROOT / "scripts" / "versions.env").read_text(), re.M))
        self.assertRegex(versions["CNPG_VERSION"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(versions["CNPG_MANIFEST_SHA256"], r"^[0-9a-f]{64}$")
        script = (ROOT / "scripts" / "dev" / "openfga-e2e.sh").read_text()
        for needed in ("CNPG_MANIFEST_SHA256", "apply --server-side", "overlays/ci-openfga", "job/openfga-migrate",
                       "inkcast authz write-model", "/readyz", "INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID"):
            self.assertIn(needed, script)
        self.assertNotIn("sha256sum -c", script, "not on macOS: compare the digest as scripts/kind-tools.sh does")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("scripts/dev/openfga-e2e.sh", ci)
        self.assertLess(ci.index("pip install -e ."), ci.index("scripts/dev/openfga-e2e.sh"), "the script runs `inkcast`")
        self.assertIn("inkcast-openfga", ci.split("name: Diagnostics")[1])


class NoSecretInTransport(unittest.TestCase):
    def test_admin_requests_carry_the_bearer_and_errors_do_not(self):
        from inkcast.libs.authz.openfga import OpenFgaAdmin
        from inkcast.libs.authz.types import EngineError
        seen = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.headers.get("authorization"))
            return httpx2.Response(503, json={"code": "unavailable", "message": "down"})
        admin = OpenFgaAdmin("http://fga.test", "k-" * 16, httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        import asyncio
        with self.assertRaises(EngineError) as caught:
            asyncio.run(admin.find_store("inkcast"))
        self.assertEqual(seen, ["Bearer " + "k-" * 16])
        self.assertNotIn("k-k-", str(caught.exception))
        self.assertIn("503", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
