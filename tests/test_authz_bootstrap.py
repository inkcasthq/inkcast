# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The OpenFGA store and model written and pinned by the deployment, with no manual step (E3.F3.9.S6, issue #315).

Two halves. The init container ``authz-model`` of Inkcast's Deployment (``components/openfga``) runs ``inkcast
authz write-model`` with the pod's own image before the server starts: every pod start re-checks or writes, the
kubelet retries it with backoff, and several pods starting together on a fresh install converge on one store
(``Convergence`` below). Inkcast's start-up, given ``INKCAST_FGA_STORE_NAME`` and no ids, resolves the store by name
and the version equal to its own packaged model (``same_model``), pins both, and refuses to start with a named error
until that version exists; explicit ids still win. Every outcome is driven against the fake OpenFGA (tests/fakes.py)
over real HTTP; the kind job (scripts/dev/openfga-e2e.sh) proves the init container on a cluster."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import httpx2
import yaml
from fakes import Fakes
from test_authz import free_port, run, serve

from inkcast.authz_cli import authz_main
from inkcast.launcher import BUILTIN, EX_CONFIG, main
from inkcast.libs.authz import build_engine, pin_model
from inkcast.libs.authz.model import PACKAGED_MODEL
from inkcast.libs.config import Config, ConfigError, load_config
from inkcast.libs.observability import Metrics

ROOT = pathlib.Path(__file__).resolve().parents[1]
KUSTOMIZE = ROOT / "deploy" / "kustomize"
INIT = "authz-model"                       # the init container (components/openfga/authz-model.yaml)
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
        self.fakes.fga_races_reset()
        self.env = {**BASE, "INKCAST_FGA_API_URL": f"{self.fakes.base}/fga", "INKCAST_FGA_API_TOKEN": self.fakes.fga_token,
                    "INKCAST_FGA_STORE_NAME": "inkcast"}

    def config(self, **env: str) -> Config:
        config = load_config({**self.env, **env})
        config.validate(("secret_key", "login", "authz"))
        return config

    def writer(self, *args: str, **env: str) -> tuple[int, str, str]:
        """What the init container does: ``inkcast authz write-model`` with the pod's settings."""
        out, err = io.StringIO(), io.StringIO()
        code = authz_main(["write-model", *args], environ={**self.env, **env}, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def job(self, *args: str, **env: str) -> tuple[str, str]:
        """A successful writer run: the ids it printed."""
        code, out, err = self.writer(*args, **env)
        self.assertEqual(code, 0, err)
        return self.ids(out)

    def ids(self, out: str) -> tuple[str, str]:
        printed = dict(line.split("=", 1) for line in out.splitlines())
        self.assertEqual(set(printed), {"INKCAST_FGA_STORE_ID", "INKCAST_FGA_MODEL_ID"}, out)
        return printed["INKCAST_FGA_STORE_ID"], printed["INKCAST_FGA_MODEL_ID"]

    def calls(self) -> list[tuple[str, str]]:
        return [(m, p.removeprefix("/fga/")) for m, p, _ in self.fakes.fga_calls]

    def refusal(self, config: Config) -> str:
        with self.assertRaises(ConfigError) as caught:
            pin_model(config)
        return str(caught.exception)

    def deleted(self) -> list[str]:
        """The store ids DELETE was asked for, in order."""
        return [p.rsplit("/", 1)[1] for m, p, _ in self.fakes.fga_calls if m == "DELETE"]


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

    def test_refuses_while_the_writer_has_not_created_the_store(self):
        message = self.refusal(self.config())
        self.assertIn("no store named 'inkcast'", message)
        self.assertIn("INKCAST_FGA_STORE_NAME", message)
        self.assertIn(INIT, message, "the error names the init container that writes it")
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
        self.assertIn(INIT, message)
        self.assertEqual(self.calls(), [("GET", "stores"), ("GET", f"stores/{store_id}/authorization-models")])
        self.assertEqual(len(self.fakes.fga_models[store_id]), 1, "nothing written")

    def test_pins_the_equal_version_under_a_newer_one(self):
        """A rollback: the newer release's version is newest, this release's is below it. Start-up pins its own
        version at once, without waiting for the writer to write it again."""
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
        self.assertIn("INKCAST_FGA_STORE_ID", message, "the remedy: pin by hand, or delete the others")
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


class Convergence(FakeOpenFga):
    """The writer runs in every Inkcast pod that checks permissions, so pods starting together on a fresh install may
    each find no store and each create one: OpenFGA's find-then-create is not atomic and store names are not unique.
    After creating, a writer lists the stores of the name again; every writer uses the oldest (store ids are ULIDs the
    server makes when it handles the create, OpenFGA v1.22.0 commands/create_store.go, so the oldest sorts first),
    and one whose own store is not the oldest deletes it before writing anything into it. A writer deletes no store
    it did not create. The model write stays idempotent: a version equal to the packaged model is not written again.
    The racers here are real: concurrent ``inkcast authz write-model`` runs against the fake, which holds each create
    until all of them have arrived and landed (``Fakes.fga_racers``)."""

    def race(self, racers: int) -> list[tuple[int, str, str]]:
        self.fakes.fga_racers = racers
        results: list = [None] * racers

        def racer(i: int) -> None:
            results[i] = self.writer()
        threads = [threading.Thread(target=racer, args=(i,), name=f"racer-{i}") for i in range(racers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([m for m, p, _ in self.fakes.fga_calls if p == "/fga/stores"].count("POST"), racers,
                         "each racer found no store and created one")
        return results

    def assert_converged(self, results: list[tuple[int, str, str]]) -> tuple[str, str]:
        """Every racer used the oldest store; the others are gone, each deleted once, by its own creator; the oldest
        holds only versions equal to the packaged model, and the next run finds one and writes nothing."""
        created = sorted(re.findall(r"store 'inkcast' created: (\w+)", "".join(err for _, _, err in results)))
        self.assertEqual(len(created), len(results), "every racer says which store it created")
        oldest = created[0]
        for code, out, err in results:
            self.assertEqual(code, 0, err)
            self.assertEqual(self.ids(out)[0], oldest, err)
        self.assertEqual(self.fakes.fga_stores, {oldest: "inkcast"})
        self.assertEqual(sorted(self.deleted()), created[1:], "each younger store deleted once, the oldest never")
        packaged = json.loads(PACKAGED_MODEL.read_text())
        versions = {m["id"]: m for m in self.fakes.fga_models[oldest]}
        self.assertTrue(versions)
        for version in versions.values():
            self.assertEqual(version["type_definitions"], packaged["type_definitions"])
        for _, out, _ in results:
            self.assertIn(self.ids(out)[1], versions, "each racer printed a version the store holds")
        self.fakes.fga_calls = []
        store_id, model_id = self.job()
        self.assertEqual((store_id, model_id), (oldest, self.fakes.fga_models[oldest][-1]["id"]))
        self.assertEqual([m for m, _, _ in self.fakes.fga_calls], ["GET", "GET"], "a later pod reads only")
        pinned = pin_model(self.config())
        self.assertEqual((pinned.fga_store_id, pinned.fga_model_id), (store_id, model_id), "the server pins the same")
        return store_id, model_id

    def test_two_racers_converge_on_the_oldest_store(self):
        results = self.race(2)
        self.assert_converged(results)
        self.assertEqual(sum("deleted" in err for _, _, err in results), 1, "the younger racer says it deleted its own")

    def test_three_racers_converge_on_the_oldest_store(self):
        self.assert_converged(self.race(3))

    def test_a_create_that_lands_late_is_deleted_by_its_own_writer(self):
        """Racer L found no store, but its create reaches OpenFGA only after racer A has created its store, written
        the model and finished: L's id is the younger (made when the create is handled), so L deletes its own and
        uses A's, where the model already is. Nothing is written twice."""
        hold = threading.Event()
        self.fakes.fga_hold_create = hold
        late: dict = {}
        thread = threading.Thread(target=lambda: late.update(zip(("code", "out", "err"), self.writer())), name="late")
        thread.start()
        deadline = time.monotonic() + 10
        while ("POST", "/fga/stores") not in [(m, p) for m, p, _ in self.fakes.fga_calls] and time.monotonic() < deadline:
            time.sleep(0.01)
        a_store, a_model = self.job()                     # A: L's create is held, so A finds none and creates its own
        hold.set()
        thread.join(30)
        self.assertEqual(late["code"], 0, late["err"])
        self.assertEqual(self.ids(late["out"]), (a_store, a_model), "L uses A's store and A's version")
        self.assertIn("model unchanged", late["err"])
        self.assertEqual(self.fakes.fga_stores, {a_store: "inkcast"}, "L's own store is gone")
        self.assertEqual(len(self.fakes.fga_models[a_store]), 1, "nothing written twice")
        (deleted,) = self.deleted()
        self.assertGreater(deleted, a_store, "the store deleted is L's, the younger")

    def test_a_create_whose_answer_is_lost_is_adopted_by_the_next_run(self):
        """The store landed, the answer did not (a timeout): this run cannot know the id, so it fails without
        touching anything; the kubelet starts the init container again, and that run finds and uses the store."""
        self.fakes.fga_create_lands_but_answers = 503
        code, out, err = self.writer()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("503", err)
        (landed,) = self.fakes.fga_stores
        self.assertEqual(self.fakes.fga_models[landed], [], "nothing written into a store this run cannot name")
        self.assertEqual(self.deleted(), [])
        store_id, _ = self.job()
        self.assertEqual(store_id, landed)
        self.assertEqual(self.fakes.fga_stores, {landed: "inkcast"}, "adopted, not created again")
        self.assertEqual(len(self.fakes.fga_models[landed]), 1)

    def test_a_writer_never_deletes_a_store_that_holds_a_version(self):
        """Two stores of the name that this writer did not create (made by hand, or left by a race this design does
        not close: a create whose id is older but which lands after a younger store's writer has listed), the younger
        holding a version: it writes into the oldest and deletes neither, because either may hold tuples. The server
        still refuses two, naming both, so the duplicate is seen."""
        older, younger = "01JFAKE0000000000000000001", "01JFAKE0000000000000000002"
        self.fakes.fga_stores = {younger: "inkcast", older: "inkcast"}
        self.fakes.fga_models = {younger: [{"id": "01JHANDMADE000000000000001", **json.loads(PACKAGED_MODEL.read_text())}],
                                 older: []}
        code, out, err = self.writer()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(out)[0], older, "the oldest, as every writer chooses")
        self.assertIn("2 stores named 'inkcast'", err)
        self.assertEqual(self.deleted(), [])
        self.assertEqual(set(self.fakes.fga_stores), {older, younger})
        self.assertEqual((len(self.fakes.fga_models[older]), len(self.fakes.fga_models[younger])), (1, 1))
        message = self.refusal(self.config())
        self.assertIn("2 stores named 'inkcast'", message)
        self.assertIn(older, message)
        self.assertIn(younger, message)

    def test_pinned_ids_mean_nothing_to_do(self):
        """K6: a deployment that pins both ids by hand runs the same init container; it creates no store and writes
        no version, and makes no call at all. One id alone pins nothing (start-up refuses it), so the writer runs."""
        code, out, err = self.writer(INKCAST_FGA_STORE_ID="01JSTORE0000000000000000AA", INKCAST_FGA_MODEL_ID="01JMODEL0000000000000000AA")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "INKCAST_FGA_STORE_ID=01JSTORE0000000000000000AA\nINKCAST_FGA_MODEL_ID=01JMODEL0000000000000000AA\n")
        self.assertIn("nothing to do", err)
        self.assertIn("INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID", err)
        self.assertEqual(self.fakes.fga_calls, [], "no call at all")
        self.assertEqual(self.fakes.fga_stores, {})
        code, out, err = self.writer(INKCAST_FGA_STORE_ID="01JSTORE0000000000000000AA")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.fakes.fga_stores), 1, "one id alone: the writer runs as usual")

    def test_a_second_run_of_the_same_release_changes_nothing(self):
        store_id, model_id = self.job()
        self.fakes.fga_calls = []
        code, out, err = self.writer()
        self.assertEqual((code, out), (0, f"INKCAST_FGA_STORE_ID={store_id}\nINKCAST_FGA_MODEL_ID={model_id}\n"))
        self.assertIn("unchanged", err)
        self.assertEqual(self.calls(), [("GET", "stores"), ("GET", f"stores/{store_id}/authorization-models")], "reads only")


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
        self.assertIn(INIT, message)

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
    """``inkcast`` (the pod's command) exits EX_CONFIG with the named reason while the store is missing, before any
    server or database is opened."""

    def test_refuses_to_start_naming_the_store_and_the_writer(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, "INKCAST_APP_NAME": "All"}, clear=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main([])
        self.assertEqual(caught.exception.code, EX_CONFIG)
        self.assertIn("refusing to start", err.getvalue())
        self.assertIn("no store named 'inkcast'", err.getvalue())
        self.assertIn(INIT, err.getvalue())
        self.assertEqual(self.calls(), [("GET", "stores")])


class Cli(FakeOpenFga):
    """``inkcast authz write-model`` reads INKCAST_FGA_STORE_NAME, so the init container and the server name one store."""

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
    """The init container as every overlay and environment with the component renders it."""

    ENVIRONMENTS = ("environments/staging", "environments/production")
    OVERLAYS = ("overlays/kubernetes", "overlays/openshift", "overlays/ci-openfga")
    OPENSHIFT = ("overlays/openshift", "environments/staging", "environments/production")

    @classmethod
    def setUpClass(cls):
        cls.docs = {name: render(name) for name in cls.ENVIRONMENTS + cls.OVERLAYS}

    def containers(self, name: str) -> tuple[dict, dict]:
        """(the server container, the init container) of Deployment inkcast."""
        pod = pod_spec(one(self.docs[name], "Deployment", "inkcast"))
        (server,) = pod["containers"]
        (init,) = pod["initContainers"]
        return server, init

    def test_the_init_container_runs_write_model_with_the_pod_s_own_image(self):
        for name in self.docs:
            with self.subTest(target=name):
                server, init = self.containers(name)
                self.assertEqual(init["name"], INIT)
                self.assertEqual(init["image"], server["image"], "one image: the `images` entry rewrites initContainers too")
                self.assertEqual(init["args"], ["authz", "write-model"])
                self.assertEqual(init["envFrom"], [{"configMapRef": {"name": "inkcast-config"}}])
                mounts = {m["name"]: m for m in init["volumeMounts"]}
                self.assertEqual(mounts["secrets"], {"name": "secrets", "mountPath": "/var/run/secrets/inkcast", "readOnly": True})
                volumes = {v["name"]: v for v in pod_spec(one(self.docs[name], "Deployment", "inkcast"))["volumes"]}
                self.assertEqual(volumes["secrets"]["secret"], {"secretName": "inkcast-secrets", "defaultMode": 0o440})
                self.assertEqual(init.get("imagePullPolicy"), server.get("imagePullPolicy"), "Never on kind, as the server")
                self.assertEqual(init["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                           "capabilities": {"drop": ["ALL"]}}, "restricted-v2, as the server")
                self.assertEqual(init["securityContext"], server["securityContext"])
                self.assertNotIn("ports", init)
                self.assertNotIn("INKCAST_SECRET_KEY_FILE", {e["name"] for e in init.get("env", [])},
                                 "the writer reads the FGA key only, not the server's secrets")
        self.assertRegex(self.containers("environments/staging")[1]["image"], r"^ghcr\.io/inkcasthq/inkcast@sha256:[0-9a-f]{64}$")
        self.assertEqual(self.containers("overlays/ci-openfga")[1]["image"], "inkcast:ci")

    def test_no_job_remains_and_the_rollout_waits_as_long_as_the_readme_says(self):
        """Every pod start runs the writer and the kubelet retries it: no Job, its TTL or its NetworkPolicy. On a fresh
        install the pod waits for OpenFGA, then for the kubelet's restart back-off (10 s doubling to 300 s, kubelet
        v1.37.0 kubelet.go initialCrashLoopBackOff and MaxContainerBackOff): the Deployment's progress deadline is
        raised from the default 600 s to 900 s, so `rollout status --timeout=15m` in the README does not fail first."""
        timeouts = [int(m) * 60 for m in re.findall(r"rollout status deploy/inkcast --timeout=(\d+)m", (ROOT / "README.md").read_text())]
        self.assertEqual(len(timeouts), 2, "Hosted §2 and the lab sequence")
        for name, docs in self.docs.items():
            with self.subTest(target=name):
                self.assertEqual([d["kind"] for d in docs if d["metadata"]["name"] == "inkcast-authz-model"], [])
                deployment = one(docs, "Deployment", "inkcast")
                self.assertEqual(deployment["spec"]["progressDeadlineSeconds"], 900)
                self.assertEqual(max(timeouts), deployment["spec"]["progressDeadlineSeconds"])
                self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})

    def test_on_openshift_the_init_container_trusts_the_injected_bundle_like_the_server(self):
        """It reads the server's configuration, so it trusts the same CAs: an OpenFGA or a secrets back-end reached
        over HTTPS works for both or for neither."""
        for name in self.docs:
            with self.subTest(target=name):
                server, init = self.containers(name)
                env = {e["name"]: e.get("value") for e in init.get("env", [])}
                server_env = {e["name"]: e.get("value") for e in server["env"]}
                mounts = {m["name"]: m for m in init["volumeMounts"]}
                if name in self.OPENSHIFT:
                    self.assertEqual(env["SSL_CERT_FILE"], server_env["SSL_CERT_FILE"])
                    self.assertEqual(mounts["trusted-ca"], {m["name"]: m for m in server["volumeMounts"]}["trusted-ca"])
                else:
                    self.assertNotIn("SSL_CERT_FILE", env)
                    self.assertNotIn("trusted-ca", mounts)

    def test_the_config_names_the_store_and_pins_no_id(self):
        for name, docs in self.docs.items():
            with self.subTest(target=name):
                data = one(docs, "ConfigMap", "inkcast-config")["data"]
                self.assertEqual(data["INKCAST_FGA_STORE_NAME"], "inkcast")
                self.assertNotIn("INKCAST_FGA_STORE_ID", data)
                self.assertNotIn("INKCAST_FGA_MODEL_ID", data)
                self.assertEqual(data["INKCAST_AUTHZ_ENGINE"], "openfga")

    def test_the_pod_s_own_policies_let_the_init_container_reach_openfga(self):
        """The init container runs in the server's pod: the base policy (DNS) and inkcast-to-openfga (8080) select
        that pod, so no policy of its own is needed."""
        for name, docs in self.docs.items():
            with self.subTest(target=name):
                labels = one(docs, "Deployment", "inkcast")["spec"]["template"]["metadata"]["labels"]
                to_openfga = one(docs, "NetworkPolicy", "inkcast-to-openfga")
                self.assertLessEqual(to_openfga["spec"]["podSelector"]["matchLabels"].items(), labels.items())
                self.assertEqual([p["port"] for rule in to_openfga["spec"]["egress"] for p in rule["ports"]], [8080])
                base = one(docs, "NetworkPolicy", "inkcast")
                self.assertLessEqual(base["spec"]["podSelector"]["matchLabels"].items(), labels.items())
                self.assertIn(53, [p["port"] for rule in base["spec"]["egress"] for p in rule["ports"]])

    def test_split_mode_gives_the_init_container_to_every_service_that_checks_permissions(self):
        """components/split-services replaces Deployment inkcast with one Deployment per service. Listed before this
        component (its header says so), every service whose processes check permissions (launcher.BUILTIN needs
        "authz") gets the init container; the Gateway, which checks nothing and mounts no key, does not."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            up = os.path.relpath(os.path.realpath(KUSTOMIZE), tmp)   # kustomize refuses absolute paths
            (pathlib.Path(tmp) / "kustomization.yaml").write_text(yaml.safe_dump({
                "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": [f"{up}/base"],
                "components": [f"{up}/components/split-services", f"{up}/components/openfga"]}))
            out = subprocess.run(["kubectl", "kustomize", tmp], capture_output=True, text=True, check=True)
        docs = [d for d in yaml.safe_load_all(out.stdout) if d]
        services = {spec.full_name: spec for spec in BUILTIN.values()}
        deployments = {d["metadata"]["labels"]["inkcast.io/service"]: d for d in docs
                       if d["kind"] == "Deployment" and d["metadata"]["labels"].get("app.kubernetes.io/name") == "inkcast"}
        self.assertEqual(len(deployments), 6)
        for service, deployment in deployments.items():
            with self.subTest(service=service):
                pod = pod_spec(deployment)
                if "authz" in services[service].needs:
                    (init,) = pod["initContainers"]
                    self.assertEqual((init["name"], init["image"]), (INIT, pod["containers"][0]["image"]))
                    self.assertEqual(deployment["spec"]["progressDeadlineSeconds"], 900)
                else:
                    self.assertNotIn("initContainers", pod)
                    self.assertNotIn("progressDeadlineSeconds", deployment["spec"])
        self.assertNotIn("initContainers", pod_spec(deployments["gateway"]), "the one service that checks nothing")


class KindProof(unittest.TestCase):
    """The kind job proves the init container end to end: a fresh install, then a second rollout, with no write-model
    run and no ConfigMap patch by hand."""

    def test_the_e2e_script_waits_for_the_pod_and_never_pins_by_hand(self):
        script = (ROOT / "scripts" / "dev" / "openfga-e2e.sh").read_text()
        for needed in (f"-c {INIT}", "rollout status deploy/inkcast", "rollout restart deploy/inkcast", "/readyz",
                       "model written", "model unchanged", "apply -k"):
            self.assertIn(needed, script)
        for gone in ("job/inkcast-authz-model", "patch configmap", "write_model()", "ttlSecondsAfterFinished"):
            self.assertNotIn(gone, script)
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn(f"-c {INIT}", ci.split("name: Diagnostics")[1])


if __name__ == "__main__":
    unittest.main()
