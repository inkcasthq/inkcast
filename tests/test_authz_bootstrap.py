# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The OpenFGA store and model written and pinned by the deployment, with no manual step (E3.F3.9.S6, issue #315).

Two halves. The Job ``inkcast-authz-model`` (``components/openfga/authz-model.yaml``) runs ``inkcast authz
write-model`` with the release's image on every apply. Inkcast's start-up, given ``INKCAST_FGA_STORE_NAME`` and no
ids, resolves the store by name and the version equal to its own packaged model (``same_model``), pins both, and
refuses to start with a named error until the Job has written them; explicit ids still win. Every outcome is
driven against the fake OpenFGA (tests/fakes.py) over real HTTP; the kind job (scripts/dev/openfga-e2e.sh) proves
the Job on a cluster."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import httpx2
import yaml
from fakes import Fakes
from test_authz import free_port, run, serve

from inkcast.authz_cli import authz_main
from inkcast.launcher import EX_CONFIG, main
from inkcast.libs.authz import build_engine, pin_model
from inkcast.libs.authz.model import PACKAGED_MODEL
from inkcast.libs.config import Config, ConfigError, load_config
from inkcast.libs.observability import Metrics

ROOT = pathlib.Path(__file__).resolve().parents[1]
KUSTOMIZE = ROOT / "deploy" / "kustomize"
JOB = "inkcast-authz-model"
BASE = {"INKCAST_SECRET_KEY": "k" * 48, "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 16,
        "INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_AUTHZ_ENGINE": "openfga"}


class FakeOpenFga(unittest.TestCase):
    """A fake OpenFGA on a port, empty before each test, and the settings a pod would hold."""

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
        self.env = {**BASE, "INKCAST_FGA_API_URL": f"{self.fakes.base}/fga", "INKCAST_FGA_API_TOKEN": self.fakes.fga_token,
                    "INKCAST_FGA_STORE_NAME": "inkcast"}

    def config(self, **env: str) -> Config:
        config = load_config({**self.env, **env})
        config.validate(("secret_key", "login", "authz"))
        return config

    def job(self, *args: str, **env: str) -> tuple[str, str]:
        """What the Job does: ``inkcast authz write-model`` with the pod's settings. The ids it printed."""
        out, err = io.StringIO(), io.StringIO()
        code = authz_main(["write-model", *args], environ={**self.env, **env}, out=out, err=err)
        self.assertEqual(code, 0, err.getvalue())
        printed = dict(line.split("=", 1) for line in out.getvalue().splitlines())
        return printed["INKCAST_FGA_STORE_ID"], printed["INKCAST_FGA_MODEL_ID"]

    def calls(self) -> list[tuple[str, str]]:
        return [(m, p.removeprefix("/fga/")) for m, p, _ in self.fakes.fga_calls]

    def refusal(self, config: Config) -> str:
        with self.assertRaises(ConfigError) as caught:
            pin_model(config)
        return str(caught.exception)


class StartUp(FakeOpenFga):
    """``pin_model``: each outcome of a pod starting with ``INKCAST_FGA_STORE_NAME`` and no ids."""

    def test_resolves_the_store_by_name_and_the_version_equal_to_the_packaged_model(self):
        store_id, model_id = self.job()
        self.fakes.fga_calls = []
        pinned = pin_model(self.config())
        self.assertEqual((pinned.fga_store_id, pinned.fga_model_id), (store_id, model_id))
        self.assertEqual(pinned.authz_engine, "openfga")
        self.assertEqual(self.calls(), [("GET", "stores"), ("GET", f"stores/{store_id}/authorization-models")],
                         "reads only: start-up never creates a store or writes a model")
        http = httpx2.AsyncClient()
        engine = build_engine(pinned, None, http, Metrics())
        self.assertEqual((engine.name, engine.engine.store_id, engine.engine.model_id), ("openfga", store_id, model_id))

        async def ready_then_close():
            try:
                return await engine.ready()
            finally:
                await http.aclose()
        self.assertIsNone(run(ready_then_close()), "the pinned version is what readiness checks")
        self.assertEqual(self.fakes.fga_stores, {store_id: "inkcast"})
        self.assertEqual(len(self.fakes.fga_models[store_id]), 1)

    def test_refuses_while_the_job_has_not_created_the_store(self):
        message = self.refusal(self.config())
        self.assertIn("no store named 'inkcast'", message)
        self.assertIn("INKCAST_FGA_STORE_NAME", message)
        self.assertIn(JOB, message, "the error names the Job that writes it")
        self.assertIn("refusing to start", message)
        self.assertEqual(self.fakes.fga_stores, {}, "start-up never creates the store: two replicas would race")
        self.assertEqual(self.calls(), [("GET", "stores")])

    def test_refuses_while_the_store_holds_only_an_older_model(self):
        older = json.loads(PACKAGED_MODEL.read_text())
        older["type_definitions"].append({"type": "team"})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(older, f)
        self.addCleanup(pathlib.Path(f.name).unlink)
        store_id, older_id = self.job("--model", f.name)
        self.fakes.fga_calls = []
        message = self.refusal(self.config())
        self.assertIn(f"store 'inkcast' ({store_id})", message)
        self.assertIn("no version equal to this release's packaged model", message)
        self.assertIn(f"1 version, the newest {older_id}", message)
        self.assertIn(JOB, message)
        self.assertEqual(self.calls(), [("GET", "stores"), ("GET", f"stores/{store_id}/authorization-models")])
        self.assertEqual(len(self.fakes.fga_models[store_id]), 1, "nothing written")

    def test_pins_the_equal_version_under_a_newer_one(self):
        """A rollback: the newer release's version is newest, this release's is below it. Start-up pins its own
        version at once, without waiting for the Job to write it again."""
        store_id, mine = self.job()
        newer = json.loads(PACKAGED_MODEL.read_text())
        newer["type_definitions"].append({"type": "team"})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(newer, f)
        self.addCleanup(pathlib.Path(f.name).unlink)
        _, newer_id = self.job("--model", f.name)
        self.assertEqual([m["id"] for m in self.fakes.fga_models[store_id]], [mine, newer_id])
        pinned = pin_model(self.config())
        self.assertEqual((pinned.fga_store_id, pinned.fga_model_id), (store_id, mine))

    def test_pages_through_every_version(self):
        """OpenFGA lists at most 100 versions a page (openfga/api, ReadAuthorizationModelsRequest.page_size <= 100):
        the equal version under 150 newer ones is still found, and the pages are asked for in order."""
        store_id, mine = self.job()
        other = {"schema_version": "1.1", "type_definitions": [{"type": "user"}, {"type": "team"}]}
        for n in range(150):
            self.fakes.fga_models[store_id].append({"id": f"01JNEWER{n:018d}", **copy.deepcopy(other)})
        self.fakes.fga_calls = []
        pinned = pin_model(self.config())
        self.assertEqual(pinned.fga_model_id, mine)
        self.assertEqual(self.calls(), [("GET", "stores")] + [("GET", f"stores/{store_id}/authorization-models")] * 2)

    def test_refuses_two_stores_of_the_same_name(self):
        self.fakes.fga_stores = {"01JFAKE0000000000000000001": "inkcast", "01JFAKE0000000000000000002": "inkcast"}
        self.fakes.fga_models = {k: [] for k in self.fakes.fga_stores}
        message = self.refusal(self.config())
        self.assertIn("2 stores named 'inkcast'", message)
        self.assertIn("INKCAST_FGA_STORE_ID", message, "the remedy: pin the one Inkcast uses")
        self.assertEqual(self.calls(), [("GET", "stores")], "neither store is read, none picked")

    def test_refuses_when_openfga_is_unreachable_and_falls_back_to_nothing(self):
        message = self.refusal(self.config(INKCAST_FGA_API_URL="http://127.0.0.1:9/fga", INKCAST_FGA_TIMEOUT="1"))
        self.assertIn("unreachable", message)
        self.assertIn("INKCAST_FGA_STORE_NAME", message)
        self.assertNotIn("local", message, "no other engine is offered")

    def test_refuses_a_wrong_key_without_printing_it(self):
        message = self.refusal(self.config(INKCAST_FGA_API_TOKEN="wrong-key"))
        self.assertIn("credentials", message)
        self.assertNotIn("wrong-key", message)
        self.assertNotIn(self.fakes.fga_token, message)

    def test_explicit_ids_win_and_nothing_is_asked(self):
        self.job()
        self.fakes.fga_calls = []
        explicit = self.config(INKCAST_FGA_STORE_ID="01JSTORE0000000000000000AA", INKCAST_FGA_MODEL_ID="01JMODEL0000000000000000AA")
        self.assertIs(pin_model(explicit), explicit)
        self.assertEqual(self.calls(), [], "ids pinned by hand are not looked up")
        local = load_config({**BASE, "INKCAST_AUTHZ_ENGINE": "local"})
        self.assertIs(pin_model(local), local)
        self.assertEqual(self.calls(), [])


class Validation(unittest.TestCase):
    """``Config.validate``: the store name replaces the pair of ids, never half of it, and is checked by OpenFGA's
    own rule (openfga/api openfga_service.proto, CreateStoreRequest.name) so a bad name is refused before any call."""

    def problem(self, **env: str) -> str:
        with self.assertRaises(ConfigError) as caught:
            load_config({**BASE, "INKCAST_FGA_API_URL": "http://openfga:8080", **env}).validate()
        return str(caught.exception)

    def test_the_name_replaces_the_pair_of_ids(self):
        load_config({**BASE, "INKCAST_FGA_API_URL": "http://openfga:8080", "INKCAST_FGA_STORE_NAME": "inkcast"}).validate()
        message = self.problem()
        self.assertIn("needs INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID", message)
        self.assertIn("INKCAST_FGA_STORE_NAME", message, "the alternative is named")
        self.assertIn(JOB, message)

    def test_half_a_pin_is_named_even_with_the_store_name_set(self):
        message = self.problem(INKCAST_FGA_STORE_NAME="inkcast", INKCAST_FGA_STORE_ID="01JSTORE0000000000000000AA")
        self.assertIn("needs INKCAST_FGA_MODEL_ID", message)
        self.assertIn("both ids unset", message, "the name resolves the pair only")
        message = self.problem(INKCAST_FGA_STORE_NAME="inkcast", INKCAST_FGA_MODEL_ID="01JMODEL0000000000000000AA")
        self.assertIn("needs INKCAST_FGA_STORE_ID", message)

    def test_the_name_follows_openfga_s_rule(self):
        for bad in ("ab", "x" * 65, "bad|name", "semi;colon"):
            with self.subTest(name=bad):
                self.assertIn("INKCAST_FGA_STORE_NAME must be 3 to 64", self.problem(INKCAST_FGA_STORE_NAME=bad))
        for good in ("inkcast", "inkcast staging & docs/eu", "a.b-c_d@e^f"):
            with self.subTest(name=good):
                load_config({**BASE, "INKCAST_FGA_API_URL": "http://openfga:8080", "INKCAST_FGA_STORE_NAME": good}).validate()

    def test_the_name_is_read_trimmed(self):
        config = load_config({**BASE, "INKCAST_FGA_API_URL": "http://openfga:8080", "INKCAST_FGA_STORE_NAME": " inkcast "})
        self.assertEqual(config.fga_store_name, "inkcast")


class Launcher(FakeOpenFga):
    """``inkcast`` (the pod's command) exits EX_CONFIG with the named reason while the Job has not run, before any
    server or database is opened."""

    def test_refuses_to_start_naming_the_store_and_the_job(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, "INKCAST_APP_NAME": "All"}, clear=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main([])
        self.assertEqual(caught.exception.code, EX_CONFIG)
        self.assertIn("refusing to start", err.getvalue())
        self.assertIn("no store named 'inkcast'", err.getvalue())
        self.assertIn(JOB, err.getvalue())
        self.assertEqual(self.calls(), [("GET", "stores")])


class Cli(FakeOpenFga):
    """``inkcast authz write-model`` reads INKCAST_FGA_STORE_NAME, so the Job and the pods name one store."""

    def test_write_model_takes_the_store_name_from_the_setting(self):
        self.job(INKCAST_FGA_STORE_NAME="inkcast staging")
        self.assertEqual(list(self.fakes.fga_stores.values()), ["inkcast staging"])
        self.job("--store", "by-flag", INKCAST_FGA_STORE_NAME="inkcast staging")
        self.assertEqual(sorted(self.fakes.fga_stores.values()), ["by-flag", "inkcast staging"], "--store wins")
        self.job(INKCAST_FGA_STORE_NAME="")
        self.assertIn("inkcast", self.fakes.fga_stores.values(), "unset: the default name")


def render(path: str) -> list[dict]:
    out = subprocess.run(["kubectl", "kustomize", str(KUSTOMIZE / path)], capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def one(docs: list[dict], kind: str, name: str) -> dict:
    (found,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return found


def pod_spec(obj: dict) -> dict:
    return obj["spec"]["template"]["spec"]


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the overlays (CI's manifests job has it)")
class Manifests(unittest.TestCase):
    """The Job as every overlay and environment with the component renders it."""

    ENVIRONMENTS = ("environments/staging", "environments/production")
    OVERLAYS = ("overlays/kubernetes", "overlays/openshift", "overlays/ci-openfga")

    @classmethod
    def setUpClass(cls):
        cls.docs = {name: render(name) for name in cls.ENVIRONMENTS + cls.OVERLAYS}

    def test_the_job_runs_write_model_with_the_release_s_image(self):
        for name, docs in self.docs.items():
            with self.subTest(target=name):
                job, server = one(docs, "Job", JOB), one(docs, "Deployment", "inkcast")
                (container,) = pod_spec(job)["containers"]
                self.assertEqual(container["image"], pod_spec(server)["containers"][0]["image"],
                                 "the same image, rewritten by the same `images` entry (digest or ci tag)")
                self.assertEqual(container["args"], ["authz", "write-model"])
                self.assertEqual(container["envFrom"], [{"configMapRef": {"name": "inkcast-config"}}])
                mounts = {m["name"]: m for m in container["volumeMounts"]}
                self.assertEqual(mounts["secrets"], {"name": "secrets", "mountPath": "/var/run/secrets/inkcast", "readOnly": True})
                volumes = {v["name"]: v for v in pod_spec(job)["volumes"]}
                self.assertEqual(volumes["secrets"]["secret"]["secretName"], "inkcast-secrets")
                self.assertEqual(job["metadata"]["namespace"], server["metadata"]["namespace"])
        staging = one(self.docs["environments/staging"], "Job", JOB)
        self.assertRegex(pod_spec(staging)["containers"][0]["image"], r"^ghcr\.io/inkcasthq/inkcast@sha256:[0-9a-f]{64}$")

    def test_the_job_is_recreated_on_every_apply_and_runs_once(self):
        """A finished Job is deleted after its TTL (the TTL-after-finished controller), so the next apply creates it
        again, with whatever image the release has: no immutable-field error. One Job, no parallelism."""
        for name, docs in self.docs.items():
            with self.subTest(target=name):
                job = one(docs, "Job", JOB)
                self.assertEqual(job["spec"]["ttlSecondsAfterFinished"], 60)
                self.assertNotIn("parallelism", job["spec"])
                self.assertNotIn("completions", job["spec"])
                self.assertEqual(pod_spec(job)["restartPolicy"], "OnFailure")
                self.assertGreaterEqual(job["spec"]["backoffLimit"], 10, "OpenFGA may take minutes on a fresh install")
                self.assertGreaterEqual(job["spec"]["activeDeadlineSeconds"], 1800)
                self.assertLessEqual(len(job["metadata"]["name"]), 63, "a Job name is a label value")

    def test_the_pod_fits_restricted_v2_runs_as_inkcast_and_pulls_like_the_server(self):
        for name, docs in self.docs.items():
            with self.subTest(target=name):
                pod = pod_spec(one(docs, "Job", JOB))
                self.assertEqual(pod["serviceAccountName"], "inkcast")
                self.assertIs(pod["automountServiceAccountToken"], False)
                self.assertIs(pod["enableServiceLinks"], False)
                self.assertEqual(pod["securityContext"].get("runAsNonRoot"), True)
                self.assertEqual(pod["securityContext"].get("seccompProfile"), {"type": "RuntimeDefault"})
                (container,) = pod["containers"]
                self.assertEqual(container["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                                "capabilities": {"drop": ["ALL"]}})
                server = pod_spec(one(docs, "Deployment", "inkcast"))
                self.assertEqual(pod.get("imagePullSecrets"), server.get("imagePullSecrets"), "ghcr-pull where the server has it")
                self.assertEqual(pod["securityContext"].get("runAsUser"), server["securityContext"].get("runAsUser"),
                                 "the uid the overlay gives the server (none on OpenShift: the SCC assigns one)")
                self.assertEqual(pod["securityContext"].get("fsGroup"), server["securityContext"].get("fsGroup"),
                                 "the mounted key is group-readable the same way")
        for name in self.ENVIRONMENTS:
            self.assertEqual(pod_spec(one(self.docs[name], "Job", JOB))["imagePullSecrets"], [{"name": "ghcr-pull"}])
            self.assertEqual(pod_spec(one(self.docs[name], "Job", JOB))["securityContext"],
                             {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}, "restricted-v2: no uid here")

    def test_the_config_names_the_store_and_pins_no_id(self):
        for name, docs in self.docs.items():
            with self.subTest(target=name):
                data = one(docs, "ConfigMap", "inkcast-config")["data"]
                self.assertEqual(data["INKCAST_FGA_STORE_NAME"], "inkcast")
                self.assertNotIn("INKCAST_FGA_STORE_ID", data)
                self.assertNotIn("INKCAST_FGA_MODEL_ID", data)
                self.assertEqual(data["INKCAST_AUTHZ_ENGINE"], "openfga")

    def test_the_job_pod_is_no_service_endpoint_and_reaches_openfga_only(self):
        for name, docs in self.docs.items():
            with self.subTest(target=name):
                job = one(docs, "Job", JOB)
                labels = job["spec"]["template"]["metadata"]["labels"]
                self.assertNotEqual(labels.get("app.kubernetes.io/component"), "server", "the Services select server pods")
                for service in ("inkcast", "inkcast-metrics"):
                    selector = one(docs, "Service", service)["spec"]["selector"]
                    self.assertFalse(selector.items() <= labels.items(), f"{service} would route to the Job's pod")
                policy = one(docs, "NetworkPolicy", JOB)
                self.assertLessEqual(policy["spec"]["podSelector"]["matchLabels"].items(), labels.items())
                self.assertEqual(policy["spec"]["policyTypes"], ["Ingress", "Egress"])
                self.assertNotIn("ingress", policy["spec"])
                ports = sorted((p["port"], p["protocol"]) for rule in policy["spec"]["egress"] for p in rule["ports"])
                self.assertEqual(ports, [(53, "TCP"), (53, "UDP"), (5353, "TCP"), (5353, "UDP"), (8080, "TCP")])
                (to_openfga,) = [rule for rule in policy["spec"]["egress"] if rule["ports"][0]["port"] == 8080]
                openfga = one(docs, "Deployment", "openfga")["spec"]["template"]["metadata"]["labels"]
                self.assertLessEqual(to_openfga["to"][0]["podSelector"]["matchLabels"].items(), openfga.items())


class KindProof(unittest.TestCase):
    """The kind job proves the Job path end to end, with no write-model run and no ConfigMap patch by hand."""

    def test_the_e2e_script_waits_for_the_job_and_never_pins_by_hand(self):
        script = (ROOT / "scripts" / "dev" / "openfga-e2e.sh").read_text()
        for needed in (f"job/{JOB}", "rollout status deploy/inkcast", "/readyz", "ttlSecondsAfterFinished", "apply -k"):
            self.assertIn(needed, script)
        self.assertNotIn("patch configmap", script, "nothing pins the ids by hand")
        self.assertNotIn("write_model()", script, "the Job runs write-model, not the script")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn(f"job/{JOB}", ci.split("name: Diagnostics")[1])


if __name__ == "__main__":
    unittest.main()
