"""The pluggable policy engine (E3.F3.6): both engines pass the same ``authz/model.fga.yaml`` that CI runs with
``fga model test``, the relation constants match the compiled model, writes are validated like OpenFGA's, and
every failure fails closed (``EngineError``)."""

from __future__ import annotations

import asyncio
import json
import pathlib
import socket
import threading
import time
import unittest

import httpx2
import uvicorn
import yaml
from fakes import Fakes

from inkcast.libs.authz import (
    MAX_DEPTH, RELATIONS, EngineError, Evaluator, Instrumented, InvalidRequest, LocalEngine, Model, OpenFgaEngine,
    Ref, Tuple, build_engine, parse_user, relations_of, user_ref,
)
from inkcast.libs.authz.model import PACKAGED_MODEL
from inkcast.libs.authz.openfga import READY_TTL
from inkcast.libs.config import Config, ConfigError, load_config
from inkcast.libs.db import Store
from inkcast.libs.observability import Metrics
from inkcast.libs.secrets import SecretValue

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODEL_JSON = ROOT / "authz" / "model.json"
MODEL_TESTS = ROOT / "authz" / "model.fga.yaml"


# One loop for the module: an httpx2 client's connections belong to the loop that opened them, and the
# OpenFGA tests share a client across many small calls.
LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


def yaml_tuples(doc: dict) -> list[Tuple]:
    return [Tuple(t["user"], t["relation"], t["object"]) for t in doc["tuples"]]


class ModelYaml:
    """Runs every ``check`` and ``list_objects`` assertion of ``model.fga.yaml`` against ``self.engine``."""

    engine = None

    def assert_yaml_passes(self, doc: dict) -> int:
        n = 0
        for case in doc["tests"]:
            for check in case.get("check", ()):
                for relation, expected in check["assertions"].items():
                    with self.subTest(test=case["name"], user=check["user"], object=check["object"], relation=relation):
                        got = run(self.engine.check(check["user"], relation, Ref.parse(check["object"])))
                        self.assertEqual(got, expected)
                        n += 1
            for lo in case.get("list_objects", ()):
                for relation, expected in lo["assertions"].items():
                    with self.subTest(test=case["name"], user=lo["user"], type=lo["type"], relation=relation):
                        got = run(self.engine.list_objects(lo["user"], relation, lo["type"]))
                        self.assertEqual(sorted(got), sorted(expected))
                        n += 1
        return n


class LocalEngineModelTests(ModelYaml, unittest.TestCase):
    """E3.F3.6.S3 acceptance: the same model.fga.yaml passes in Python."""

    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.engine = LocalEngine(self.store)
        self.doc = yaml.safe_load(MODEL_TESTS.read_text())
        run(self.engine.write(yaml_tuples(self.doc)))

    def test_model_yaml_passes(self):
        self.assertGreater(self.assert_yaml_passes(self.doc), 50)

    def test_batch_check_answers_in_order(self):
        items = [("user:alice", "can_publish", Ref("channel", "acme-x")), ("user:carol", "can_publish", Ref("channel", "acme-x")),
                 ("user:dave", "can_approve", Ref("post", "post-1"))]
        self.assertEqual(run(self.engine.batch_check(items)), [True, False, True])
        with self.assertRaises(InvalidRequest):
            run(self.engine.batch_check(items * 20))

    def test_read_filters(self):
        by_object = run(self.engine.read(obj=Ref("workspace", "acme-blog")))
        self.assertIn(Tuple("organization:agency-x#member", "editor", "workspace:acme-blog"), by_object)
        self.assertTrue(all(t.object == "workspace:acme-blog" for t in by_object))
        self.assertEqual(run(self.engine.read(user="user:erin", type_="post")), [Tuple("user:erin", "author", "post:post-1")])
        self.assertEqual(run(self.engine.read(obj=Ref("post", "post-1"), relation="workspace")),
                         [Tuple("workspace:acme-blog", "workspace", "post:post-1")])
        self.assertEqual(len(run(self.engine.read())), len(self.doc["tuples"]))
        with self.assertRaises(InvalidRequest):
            run(self.engine.read(relation="workspace"))

    def test_write_then_delete_is_atomic_and_follows_openfga(self):
        grant = Tuple("user:zed", "viewer", "workspace:acme-blog")
        self.assertFalse(run(self.engine.check("user:zed", "can_view", Ref("post", "post-1"))))
        run(self.engine.write([grant]))
        self.assertTrue(run(self.engine.check("user:zed", "can_view", Ref("post", "post-1"))))
        with self.assertRaisesRegex(InvalidRequest, "already exists"):      # OpenFGA's rule, and all-or-nothing
            run(self.engine.write([Tuple("user:zed2", "viewer", "workspace:acme-blog"), grant]))
        self.assertEqual(run(self.engine.read(user="user:zed2", type_="workspace")), [])
        run(self.engine.write(deletes=[grant]))
        self.assertFalse(run(self.engine.check("user:zed", "can_view", Ref("post", "post-1"))))
        with self.assertRaisesRegex(InvalidRequest, "does not exist"):
            run(self.engine.write(deletes=[grant]))
        with self.assertRaisesRegex(InvalidRequest, "at least one"):
            run(self.engine.write())
        with self.assertRaisesRegex(InvalidRequest, "twice"):
            run(self.engine.write([grant, grant]))
        self.assertIsNone(run(self.engine.ready()))

    def test_writes_are_validated_against_the_model(self):
        """What OpenFGA's Write refuses: a user type the relation does not list, a userset where only users go,
        a computed relation, an unknown relation or type."""
        for user, relation, obj, why in (
            ("organization:acme#member", "owner", "organization:globex", "owner takes users only"),
            ("role:r#assignee", "editor", "workspace:acme-blog", "editor takes user or organization#member"),
            ("user:x", "role_editor", "workspace:acme-blog", "role_editor takes role#assignee only"),
            ("user:x", "can_edit", "post:post-1", "can_edit is computed, not assignable"),
            ("user:x", "nope", "post:post-1", "unknown relation"),
            ("user:x", "viewer", "thing:1", "unknown type"),
            ("thing:1", "viewer", "workspace:acme-blog", "unknown user type"),
            ("organization:acme#nope", "editor", "workspace:acme-blog", "unknown userset relation"),
            ("user:*", "viewer", "workspace:acme-blog", "no wildcard in this model"),
        ):
            with self.subTest(why=why), self.assertRaises(InvalidRequest):
                run(self.engine.write([Tuple(user, relation, obj)]))

    def test_unknown_relation_type_or_user_fails_closed(self):
        with self.assertRaisesRegex(InvalidRequest, "post#nope"):
            run(self.engine.check("user:alice", "nope", Ref("post", "post-1")))
        with self.assertRaisesRegex(InvalidRequest, "type 'thing'"):
            run(self.engine.check("user:alice", "can_view", Ref("thing", "1")))
        with self.assertRaisesRegex(InvalidRequest, "type 'thing'"):
            run(self.engine.check("thing:1", "can_view", Ref("post", "post-1")))
        with self.assertRaises(InvalidRequest):
            run(self.engine.check("alice", "can_view", Ref("post", "post-1")))
        with self.assertRaises(InvalidRequest):
            run(self.engine.list_objects("user:alice", "nope", "post"))
        with self.assertRaises(InvalidRequest):
            run(self.engine.list_objects("user:alice", "can_view", "thing"))
        self.assertTrue(issubclass(InvalidRequest, EngineError))

    def test_a_userset_subject_can_be_checked(self):
        """``organization:agency-x#member`` holds editor on the blog directly, and so can_create_post."""
        self.assertTrue(run(self.engine.check("organization:agency-x#member", "can_create_post", Ref("workspace", "acme-blog"))))
        self.assertFalse(run(self.engine.check("organization:agency-y#member", "can_create_post", Ref("workspace", "acme-blog"))))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, f"the fake OpenFGA on port {port} did not start"
        time.sleep(0.02)
    return server


class OpenFgaEngineModelTests(ModelYaml, unittest.TestCase):
    """E3.F3.6.S2: the same model.fga.yaml, through OpenFgaEngine -> real HTTP -> the fake OpenFGA -> the
    evaluator. The fake validates the request shapes against OpenFGA's documented API."""

    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.fakes = Fakes(f"http://127.0.0.1:{port}")
        cls.server = serve(cls.fakes.app(), port)
        cls.http = httpx2.AsyncClient()

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        run(cls.http.aclose())

    def setUp(self):
        self.fakes.fga_tuples, self.fakes.fga_calls, self.fakes.fga_fail_next = [], [], None
        self.engine = OpenFgaEngine(f"{self.fakes.base}/fga", self.fakes.fga_store, self.fakes.fga_model,
                                    self.fakes.fga_token, self.http, timeout=5.0)
        self.doc = yaml.safe_load(MODEL_TESTS.read_text())
        run(self.engine.write(yaml_tuples(self.doc)))

    def test_model_yaml_passes_over_http(self):
        n = self.assert_yaml_passes(self.doc)
        self.assertGreater(n, 50)
        checks = [b for m, path, b in self.fakes.fga_calls if path.endswith("/check")]
        self.assertEqual(len(checks), n - 4)  # four list_objects assertions in the yaml
        self.assertEqual(checks[0]["authorization_model_id"], self.fakes.fga_model)   # every call pins the model
        self.assertEqual(checks[0]["consistency"], "MINIMIZE_LATENCY")

    def test_batch_check_uses_correlation_ids_and_chunks(self):
        items = [("user:alice", "can_publish", Ref("channel", "acme-x")), ("user:carol", "can_publish", Ref("channel", "acme-x")),
                 ("user:dave", "can_approve", Ref("post", "post-1"))] * 20
        self.assertEqual(run(self.engine.batch_check(items)), [True, False, True] * 20)
        batches = [b for m, path, b in self.fakes.fga_calls if path.endswith("/batch-check")]
        self.assertEqual([len(b["checks"]) for b in batches], [50, 10])
        self.assertEqual(batches[0]["checks"][1]["correlation_id"], "1")
        self.assertEqual(run(self.engine.batch_check([])), [])
        with self.assertRaises(InvalidRequest):
            run(self.engine.batch_check([("user:alice", "nope", Ref("post", "post-1"))]))

    def test_consistency_is_passed_through(self):
        run(self.engine.check("user:alice", "can_edit", Ref("post", "post-1"), consistency="higher_consistency"))
        self.assertEqual(self.fakes.fga_calls[-1][2]["consistency"], "HIGHER_CONSISTENCY")
        with self.assertRaises(InvalidRequest):
            run(self.engine.check("user:alice", "can_edit", Ref("post", "post-1"), consistency="eventual"))

    def test_read_pages_through_continuation_tokens(self):
        everything = run(self.engine.read())
        self.assertEqual(len(everything), len(self.doc["tuples"]))
        self.assertEqual(run(self.engine.read(user="user:erin", type_="post")), [Tuple("user:erin", "author", "post:post-1")])
        self.assertEqual(self.fakes.fga_calls[-1][2]["tuple_key"], {"object": "post:", "user": "user:erin"})
        run(self.engine.write([Tuple("user:zed", "viewer", "workspace:acme-blog")]))
        run(self.engine.write(deletes=[Tuple("user:zed", "viewer", "workspace:acme-blog")]))
        with self.assertRaisesRegex(InvalidRequest, "does not exist"):
            run(self.engine.write(deletes=[Tuple("user:zed", "viewer", "workspace:acme-blog")]))
        with self.assertRaisesRegex(InvalidRequest, "already exists"):
            run(self.engine.write([Tuple("user:erin", "author", "post:post-1")]))
        with self.assertRaises(InvalidRequest):
            run(self.engine.write([Tuple("organization:acme#member", "owner", "organization:acme")]))
        with self.assertRaises(InvalidRequest):
            run(self.engine.read(user="user:erin"))
        with self.assertRaises(InvalidRequest):
            run(self.engine.write([Tuple(f"user:u{i}", "viewer", "workspace:acme-blog") for i in range(101)]))
        self.fakes.fga_tuples = [(f"user:u{i}", "viewer", "workspace:acme-blog") for i in range(250)]
        self.assertEqual(len(run(self.engine.read(obj=Ref("workspace", "acme-blog")))), 250)
        reads = [b for m, path, b in self.fakes.fga_calls if path.endswith("/read")]
        self.assertEqual([b.get("continuation_token") for b in reads[-3:]], [None, "100", "200"])

    def test_readiness_checks_the_pinned_model_and_is_cached(self):
        clock = [1000.0]
        engine = OpenFgaEngine(f"{self.fakes.base}/fga", self.fakes.fga_store, self.fakes.fga_model,
                               self.fakes.fga_token, self.http, clock=lambda: clock[0])
        self.assertIsNone(run(engine.ready()))
        self.assertIsNone(run(engine.ready()))
        self.assertEqual(sum(1 for m, p, _ in self.fakes.fga_calls if m == "GET"), 1, "cached")
        clock[0] += READY_TTL + 1
        self.assertIsNone(run(engine.ready()))
        self.assertEqual(sum(1 for m, p, _ in self.fakes.fga_calls if m == "GET"), 2)
        wrong = OpenFgaEngine(f"{self.fakes.base}/fga", self.fakes.fga_store, "01JNOPE", self.fakes.fga_token, self.http)
        self.assertIn("authorization_model_not_found", run(wrong.ready()))
        wrong_store = OpenFgaEngine(f"{self.fakes.base}/fga", "01JNOPE", self.fakes.fga_model, self.fakes.fga_token, self.http)
        self.assertIn("404", run(wrong_store.ready()))
        with self.assertRaisesRegex(EngineError, "not found"):
            run(wrong_store.check("user:alice", "can_edit", Ref("post", "post-1")))
        with self.assertRaisesRegex(InvalidRequest, "authorization_model_not_found"):
            run(wrong.check("user:alice", "can_edit", Ref("post", "post-1")))

    def test_every_failure_fails_closed(self):
        no_key = OpenFgaEngine(f"{self.fakes.base}/fga", self.fakes.fga_store, self.fakes.fga_model, "", self.http)
        with self.assertRaisesRegex(EngineError, "credentials"):
            run(no_key.check("user:alice", "can_edit", Ref("post", "post-1")))
        self.assertNotIn(self.fakes.fga_token, str(self.engine.token))
        for status in (500, 503, 429):
            self.fakes.fga_fail_next = status
            with self.subTest(status=status), self.assertRaisesRegex(EngineError, str(status)):
                run(self.engine.check("user:alice", "can_edit", Ref("post", "post-1")))
        self.fakes.fga_fail_next = 503
        with self.assertRaises(EngineError):
            run(self.engine.write([Tuple("user:zed", "viewer", "workspace:acme-blog")]))
        self.assertEqual(run(self.engine.read(user="user:zed", type_="workspace")), [], "nothing was written")
        with self.assertRaises(EngineError):
            OpenFgaEngine("", "s", "m")


class OpenFgaTransport(unittest.TestCase):
    """What the fake can't do: connection errors (retried once for reads, never for writes), timeouts, bad bodies."""

    def engine(self, handler):
        return OpenFgaEngine("http://fga.test", "s1", "m1", "k", httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))

    def test_reads_retry_once_on_connection_errors_and_writes_never(self):
        calls = []

        def flaky(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                raise httpx2.ConnectError("refused")
            return httpx2.Response(200, json={"allowed": True})

        self.assertTrue(run(self.engine(flaky).check("user:a", "can_view", Ref("post", "1"))))
        self.assertEqual(len(calls), 2)

        def down(request):
            calls.append(request.url.path)
            raise httpx2.ConnectError("refused")

        calls.clear()
        with self.assertRaisesRegex(EngineError, "unreachable"):
            run(self.engine(down).write([Tuple("user:a", "viewer", "workspace:w")]))
        self.assertEqual(len(calls), 1, "a write is never retried blindly")
        calls.clear()
        with self.assertRaisesRegex(EngineError, "unreachable"):
            run(self.engine(down).check("user:a", "can_view", Ref("post", "1")))
        self.assertEqual(len(calls), 2)

    def test_timeouts_and_malformed_answers_are_engine_errors(self):
        def slow(request):
            raise httpx2.ReadTimeout("slow")

        with self.assertRaisesRegex(EngineError, "ReadTimeout"):
            run(self.engine(slow).check("user:a", "can_view", Ref("post", "1")))
        for body in (b"not json", b'{"nope": 1}', b'{"allowed": "yes"}'):
            with self.subTest(body=body), self.assertRaises(EngineError):
                run(self.engine(lambda r, b=body: httpx2.Response(200, content=b, headers={"content-type": "application/json"}))
                    .check("user:a", "can_view", Ref("post", "1")))
        with self.assertRaises(EngineError):
            run(self.engine(lambda r: httpx2.Response(200, json={"result": {"0": {"error": {"input_error": "validation_error", "message": "x"}}}}))
                .batch_check([("user:a", "can_view", Ref("post", "1"))]))
        with self.assertRaises(EngineError):
            run(self.engine(lambda r: httpx2.Response(200, json={})).list_objects("user:a", "can_view", "post"))
        self.assertIn("HTTP 500", run(self.engine(lambda r: httpx2.Response(500, content=b"oops")).ready()))

    def test_the_bearer_header_carries_the_key_and_nothing_logs_it(self):
        seen = {}

        def record(request):
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, json={"allowed": False})

        self.assertFalse(run(self.engine(record).check("user:a", "can_view", Ref("post", "1"))))
        self.assertEqual(seen["auth"], "Bearer k")
        self.assertEqual(seen["body"]["tuple_key"], {"user": "user:a", "relation": "can_view", "object": "post:1"})


class EvaluatorEdges(unittest.TestCase):
    """Rewrite semantics the Inkcast model does not exercise, on small synthetic models."""

    class Memory:
        def __init__(self, tuples):
            self.tuples = list(tuples)

        def users_of(self, obj, relation):
            return [u for u, r, o in self.tuples if o == obj and r == relation]

        def objects_of_type(self, type_):
            return [o for _, _, o in self.tuples if o.startswith(type_ + ":")]

    @staticmethod
    def model(**types):
        return Model({"schema_version": "1.1", "type_definitions": [{"type": "user"}, *types.values()]})

    def test_depth_limit_is_an_error_not_a_wrong_answer(self):
        node = {"type": "node", "relations": {
            "parent": {"this": {}},
            "viewer": {"union": {"child": [{"this": {}}, {"tupleToUserset": {"tupleset": {"relation": "parent"},
                                                                           "computedUserset": {"relation": "viewer"}}}]}}},
            "metadata": {"relations": {"parent": {"directly_related_user_types": [{"type": "node"}]},
                                       "viewer": {"directly_related_user_types": [{"type": "user"}]}}}}
        chain = [(f"node:{i + 1}", "parent", f"node:{i}") for i in range(40)] + [("user:u", "viewer", "node:40")]
        ev = Evaluator(self.model(node=node), self.Memory(chain))
        self.assertTrue(ev.check("user:u", "viewer", "node:30"))     # 10 hops: fine
        with self.assertRaisesRegex(EngineError, "too deep"):
            ev.check("user:u", "viewer", "node:0")                   # 40 hops: more than MAX_DEPTH
        self.assertEqual(MAX_DEPTH, 25)

    def test_cycles_grant_nothing(self):
        group = {"type": "group", "relations": {"member": {"this": {}}},
                 "metadata": {"relations": {"member": {"directly_related_user_types": [{"type": "user"}, {"type": "group", "relation": "member"}]}}}}
        ev = Evaluator(self.model(group=group), self.Memory([("group:a#member", "member", "group:b"),
                                                             ("group:b#member", "member", "group:a"),
                                                             ("user:u", "member", "group:a")]))
        self.assertTrue(ev.check("user:u", "member", "group:b"))
        self.assertFalse(ev.check("user:v", "member", "group:b"))

    def test_difference_and_wildcard(self):
        doc = {"type": "doc", "relations": {
            "viewer": {"this": {}}, "blocked": {"this": {}},
            "can_view": {"difference": {"base": {"computedUserset": {"relation": "viewer"}},
                                        "subtract": {"computedUserset": {"relation": "blocked"}}}}},
            "metadata": {"relations": {"viewer": {"directly_related_user_types": [{"type": "user"}, {"type": "user", "wildcard": {}}]},
                                       "blocked": {"directly_related_user_types": [{"type": "user"}]}}}}
        model = self.model(doc=doc)
        ev = Evaluator(model, self.Memory([("user:*", "viewer", "doc:1"), ("user:bad", "blocked", "doc:1")]))
        self.assertTrue(ev.check("user:anyone", "can_view", "doc:1"))
        self.assertFalse(ev.check("user:bad", "can_view", "doc:1"))
        model.validate_tuple(Tuple("user:*", "viewer", "doc:1"))
        with self.assertRaises(InvalidRequest):
            model.validate_tuple(Tuple("user:*", "blocked", "doc:1"))
        with self.assertRaises(InvalidRequest):
            model.validate_tuple(Tuple("user:x", "can_view", "doc:1"))

    def test_list_objects_is_capped(self):
        from inkcast.libs.authz import MAX_LIST_OBJECTS
        doc = {"type": "doc", "relations": {"viewer": {"this": {}}},
               "metadata": {"relations": {"viewer": {"directly_related_user_types": [{"type": "user"}]}}}}
        ev = Evaluator(self.model(doc=doc), self.Memory([("user:u", "viewer", f"doc:{i:05d}") for i in range(MAX_LIST_OBJECTS + 5)]))
        self.assertEqual(len(ev.list_objects("user:u", "viewer", "doc")), MAX_LIST_OBJECTS)

    def test_a_model_without_schema_1_1_is_refused(self):
        with self.assertRaises(EngineError):
            Model({"schema_version": "1.0", "type_definitions": []})
        with self.assertRaises(EngineError):
            Model.load("/nonexistent/model.json")


class Vocabulary(unittest.TestCase):
    def test_relation_constants_match_the_compiled_model(self):
        model = json.loads(MODEL_JSON.read_text())
        in_model = {td["type"]: set(td.get("relations", {})) for td in model["type_definitions"] if td.get("relations")}
        in_code = {name: relations_of(cls) for name, cls in RELATIONS.items()}
        self.assertEqual(in_code, in_model, "src/inkcast/libs/authz/types.py drifted from authz/model.json")

    def test_the_packaged_model_is_the_compiled_model(self):
        self.assertEqual(json.loads(PACKAGED_MODEL.read_text()), json.loads(MODEL_JSON.read_text()),
                         "cp authz/model.json src/inkcast/libs/authz/model.json")

    def test_refs_and_tuples_follow_openfga_identifier_rules(self):
        self.assertEqual(str(Ref("post", "post_1")), "post:post_1")
        self.assertEqual(Ref.parse("organization:acme"), Ref("organization", "acme"))
        self.assertEqual(user_ref("usr_1"), "user:usr_1")
        for bad in ("a b", "a:b", "a#b", "", "x" * 257):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Ref("post", bad)
        with self.assertRaises(ValueError):
            Ref("post:x", "1")
        with self.assertRaises(ValueError):
            Ref.parse("post")
        self.assertEqual(parse_user("organization:acme#member"), ("organization", "acme", "member"))
        self.assertEqual(parse_user("user:*"), ("user", "*", None))
        for bad in ("alice", "user:a b", "user:a#", "user:a#b#c"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_user(bad)
        t = Tuple.of("user:a", "viewer", Ref("workspace", "w"))
        self.assertEqual(t.as_key(), {"user": "user:a", "relation": "viewer", "object": "workspace:w"})
        self.assertEqual(Tuple.from_key(t.as_key()), t)
        self.assertEqual(t.ref, Ref("workspace", "w"))
        with self.assertRaises(ValueError):
            Tuple("user:a", "bad relation", "workspace:w")


if __name__ == "__main__":
    unittest.main()


class Selection(unittest.TestCase):
    """E3.F3.6.S4: INKCAST_AUTHZ_ENGINE, its settings, the production rule, readiness."""

    BASE = {"INKCAST_SECRET_KEY": "k" * 48, "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 16,
            "INKCAST_PUBLIC_URL": "https://inkcast.example.com"}

    def cfg(self, **env) -> Config:
        return load_config({**self.BASE, **env})

    def test_local_is_the_default_and_keeps_existing_deployments_working(self):
        cfg = self.cfg()
        self.assertEqual(cfg.authz_engine, "local")
        cfg.validate(("login", "secret_key", "authz"))
        self.cfg(INKCAST_ENV="ci").validate()

    def test_staging_refuses_local_like_production(self):
        """Owner's decision (PR #70 review): staging rehearses production, so the posture rule covers it too."""
        for env in ("staging", "production"):
            with self.subTest(env=env), self.assertRaisesRegex(ConfigError, f"refused in {env}"):
                self.cfg(INKCAST_ENV=env).validate()
        self.cfg(INKCAST_ENV="staging", INKCAST_AUTHZ_ALLOW_LOCAL="true").validate()

    def test_production_refuses_local_unless_a_self_host_opts_in(self):
        with self.assertRaisesRegex(ConfigError, "INKCAST_AUTHZ_ALLOW_LOCAL=true"):
            self.cfg(INKCAST_ENV="production").validate()
        self.cfg(INKCAST_ENV="production", INKCAST_AUTHZ_ALLOW_LOCAL="true").validate()
        gw = self.cfg(INKCAST_ENV="production", INKCAST_UPSTREAM_PUBLISHING_API="http://publishing-api:8787")
        gw.validate(("upstreams",))

    def test_openfga_needs_its_settings_and_reads_the_key_through_the_secrets_design(self):
        with self.assertRaisesRegex(ConfigError, "INKCAST_FGA_API_URL, INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID"):
            self.cfg(INKCAST_AUTHZ_ENGINE="openfga").validate()
        with self.assertRaisesRegex(ConfigError, "http\\(s\\) URL"):
            self.cfg(INKCAST_AUTHZ_ENGINE="openfga", INKCAST_FGA_API_URL="fga:8080", INKCAST_FGA_STORE_ID="s",
                     INKCAST_FGA_MODEL_ID="m").validate()
        with self.assertRaisesRegex(ConfigError, "INKCAST_AUTHZ_ENGINE must be one of"):
            self.cfg(INKCAST_AUTHZ_ENGINE="opa").validate()
        with self.assertRaisesRegex(ConfigError, "INKCAST_FGA_TIMEOUT must be a positive"):
            self.cfg(INKCAST_AUTHZ_ENGINE="openfga", INKCAST_FGA_API_URL="http://fga:8080", INKCAST_FGA_STORE_ID="s",
                     INKCAST_FGA_MODEL_ID="m", INKCAST_FGA_TIMEOUT="0").validate()
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as f:
            f.write("fga-key-from-file\n")
        self.addCleanup(pathlib.Path(f.name).unlink)
        cfg = self.cfg(INKCAST_ENV="production", INKCAST_AUTHZ_ENGINE="openfga", INKCAST_FGA_API_URL="http://fga:8080/",
                       INKCAST_FGA_STORE_ID="01JSTORE", INKCAST_FGA_MODEL_ID="01JMODEL", INKCAST_FGA_API_TOKEN_FILE=f.name)
        cfg.validate()
        self.assertEqual(cfg.fga_api_url, "http://fga:8080")
        self.assertIsInstance(cfg.fga_api_token, SecretValue)
        self.assertEqual(cfg.fga_api_token, "fga-key-from-file")
        self.assertNotIn("fga-key-from-file", repr(cfg))
        self.assertFalse(self.cfg().fga_api_token, "the key is only fetched when OpenFGA is the engine")
        missing = self.cfg(INKCAST_AUTHZ_ENGINE="openfga", INKCAST_FGA_API_URL="http://fga:8080", INKCAST_FGA_STORE_ID="s",
                           INKCAST_FGA_MODEL_ID="m", INKCAST_FGA_API_TOKEN_FILE="/nonexistent/fga.key")
        with self.assertRaisesRegex(ConfigError, "INKCAST_FGA_API_TOKEN can't be read"):
            missing.validate()
        missing.validate(())  # a service that never checks permissions (a Gateway) does not need the key

    def test_build_engine_picks_and_instruments_the_engine(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        engine = build_engine(self.cfg(), store, None, Metrics())
        self.assertIsInstance(engine, Instrumented)
        self.assertEqual(engine.name, "local")
        self.assertIsInstance(engine.engine, LocalEngine)
        self.assertIsNone(run(engine.ready()))
        cfg = self.cfg(INKCAST_AUTHZ_ENGINE="openfga", INKCAST_FGA_API_URL="http://fga:8080", INKCAST_FGA_STORE_ID="s",
                       INKCAST_FGA_MODEL_ID="m", INKCAST_FGA_API_TOKEN="k", INKCAST_FGA_TIMEOUT="2.5")
        engine = build_engine(cfg, store, httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda r: httpx2.Response(200, json={"authorization_model": {"id": "m"}}))), Metrics())
        self.assertEqual((engine.name, engine.engine.model_id, engine.engine.timeout.read), ("openfga", "m", 2.5))
        self.assertIsNone(run(engine.ready()))
        with self.assertRaisesRegex(ConfigError, "can't be loaded"):
            build_engine(self.cfg(INKCAST_AUTHZ_MODEL="/nonexistent/model.json"), store, None)

    def test_context_and_readiness_carry_the_engine(self):
        from inkcast.libs.http.app import readiness
        from inkcast.libs.runtime import build_context
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 48, database_path=":memory:")
        ctx = build_context(cfg)
        self.addCleanup(ctx.store.close)
        self.assertEqual(ctx.authz.name, "local")
        self.assertEqual(run(readiness([], ctx)), [])
        self.assertIsNone(build_context(cfg, storage=False).authz, "a Gateway has no engine")
        down = Config(public_url="http://localhost:8787", secret_key="k" * 48, database_path=":memory:",
                      authz_engine="openfga", fga_api_url="http://fga:8080", fga_store_id="s", fga_model_id="m")
        ctx = build_context(down, transport=httpx2.MockTransport(lambda r: httpx2.Response(503, json={"code": "x"})))
        self.addCleanup(ctx.store.close)
        self.assertEqual([p[:33] for p in run(readiness([], ctx))], ["authz (openfga): OpenFGA answered"])


class Instrumentation(unittest.TestCase):
    """E3.F3.6.S5: the same counters, histogram and events whichever engine answers."""

    class Flaky:
        name = "fake"

        def __init__(self):
            self.fail = False

        async def check(self, user, relation, obj, *, consistency="minimize_latency"):
            if self.fail:
                raise EngineError("down")
            return user == "user:alice"

        async def batch_check(self, items, *, consistency="minimize_latency"):
            return [await self.check(*i) for i in items]

        async def list_objects(self, user, relation, type_, *, consistency="minimize_latency"):
            if self.fail:
                raise EngineError("down")
            return ["post:1"]

        async def write(self, writes=(), deletes=()):
            if self.fail:
                raise EngineError("down")

        async def read(self, *, user=None, relation=None, obj=None, type_=None):
            return []

        async def ready(self):
            return "nope" if self.fail else None

    def setUp(self):
        self.metrics = Metrics()
        self.metrics.counter("inkcast_authz_checks_total", "")
        self.metrics.histogram("inkcast_authz_duration_seconds", "")
        self.inner = self.Flaky()
        self.engine = Instrumented(self.inner, self.metrics)

    def counts(self):
        return {k: v for k, v in self.metrics.snapshot().get("inkcast_authz_checks_total", {}).items()}

    def test_allowed_denied_and_error_are_counted_and_logged(self):
        post = Ref("post", "1")
        with self.assertNoLogs("inkcast.authz"):
            self.assertTrue(run(self.engine.check("user:alice", "can_view", post)))
        with self.assertLogs("inkcast.authz", "WARNING") as logs:
            self.assertFalse(run(self.engine.check("user:bob", "can_view", post)))
        self.assertIn("denied: user:bob lacks can_view on post:1", logs.output[0])
        self.assertEqual(logs.records[0].__dict__["event"], "authz.denied")
        self.assertEqual((logs.records[0].__dict__["user"], logs.records[0].__dict__["object"]), ("user:bob", "post:1"))
        self.inner.fail = True
        with self.assertLogs("inkcast.authz", "ERROR") as logs, self.assertRaises(EngineError):
            run(self.engine.check("user:bob", "can_view", post))
        self.assertEqual(logs.records[0].__dict__["event"], "authz.error")
        self.assertEqual(logs.records[0].__dict__["engine"], "fake")
        self.assertEqual(self.counts(), {
            (("relation", "can_view"), ("result", "allowed")): 1,
            (("relation", "can_view"), ("result", "denied")): 1,
            (("relation", "can_view"), ("result", "error")): 1,
        })
        rendered = self.metrics.render()
        self.assertIn('inkcast_authz_duration_seconds_count{engine="fake",op="check"} 3', rendered)
        self.assertEqual(self.engine.name, "fake")
        self.assertEqual(run(self.engine.ready()), "nope")

    def test_batch_list_write_and_read_are_covered_too(self):
        items = [("user:alice", "can_edit", Ref("post", "1")), ("user:bob", "can_edit", Ref("post", "1"))]
        with self.assertLogs("inkcast.authz", "WARNING") as logs:
            self.assertEqual(run(self.engine.batch_check(items)), [True, False])
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(run(self.engine.list_objects("user:alice", "can_view", "post")), ["post:1"])
        run(self.engine.write([Tuple("user:a", "viewer", "workspace:w")]))
        self.assertEqual(run(self.engine.read(type_="post")), [])
        self.inner.fail = True
        for call in (lambda: self.engine.batch_check(items), lambda: self.engine.list_objects("user:a", "can_view", "post"),
                     lambda: self.engine.write([Tuple("user:a", "viewer", "workspace:w")])):
            with self.assertLogs("inkcast.authz", "ERROR") as logs, self.assertRaises(EngineError):
                run(call())
            self.assertEqual(logs.records[0].__dict__["event"], "authz.error")
        self.assertEqual(self.counts()[(("relation", "can_edit"), ("result", "error"))], 2)
        self.assertEqual(self.counts()[(("relation", "can_view"), ("result", "error"))], 1)
        rendered = self.metrics.render()
        for op in ("batch_check", "list_objects", "write", "read"):
            self.assertIn(f'op="{op}"', rendered)


class Overlays(unittest.TestCase):
    """E3.F3.6.S4 in the manifests: the self-host overlays accept the in-process engine until they ship OpenFGA
    (E3.F3.9); the hosted environment never does, even though it builds on the openshift overlay."""

    def literals(self, path: str) -> list[str]:
        import yaml
        k = yaml.safe_load((ROOT / "deploy" / "kustomize" / path / "kustomization.yaml").read_text())
        return [lit for g in k.get("configMapGenerator", []) if g["name"] == "inkcast-config"
                for lit in g.get("literals", [])]

    def test_self_host_overlays_allow_the_local_engine(self):
        for overlay in ("overlays/openshift", "overlays/kubernetes"):
            self.assertIn("INKCAST_AUTHZ_ALLOW_LOCAL=true", self.literals(overlay), overlay)

    def test_the_hosted_environments_refuse_it(self):
        for env in ("environments/production", "environments/staging"):
            self.assertIn("INKCAST_AUTHZ_ALLOW_LOCAL=false", self.literals(env), env)

