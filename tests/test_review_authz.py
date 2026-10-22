# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of authorization (E3.F3.6): each test failed on the code it was written against and
names the behaviour it pins. OpenFGA references are to its v1.22.0 sources."""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx2

from inkcast.libs.authz import (
    EngineError, Evaluator, Instrumented, LocalEngine, Model, OpenFgaEngine, Ref, Tuple, parse_user,
)
from inkcast.libs.authz.model import PACKAGED_MODEL
from inkcast.libs.config import ConfigError, load_config
from inkcast.libs.db import Store
from inkcast.libs.observability import Metrics

LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


class Memory:
    """A TupleSource over a list of (user, relation, object)."""

    def __init__(self, tuples):
        self.tuples = list(tuples)

    def users_of(self, obj, relation):
        return [u for u, r, o in self.tuples if o == obj and r == relation]

    def objects_of_type(self, type_):
        return [o for _, _, o in self.tuples if o.startswith(type_ + ":")]


def packaged() -> dict:
    return json.loads(PACKAGED_MODEL.read_text())


class StaleTuplesAfterAModelChange(unittest.TestCase):
    """OpenFGA's check skips stored tuples its (pinned) model no longer allows: internal/graph/check.go runs
    ``validation.ValidateTupleForRead`` / ``FilterInvalidTuples`` on every direct, userset and tupleset tuple it
    reads. So a model version that drops a user type revokes those grants. The local engine must do the same,
    or tightening the model leaves the old grants working on every self-host."""

    def test_a_userset_grant_the_new_model_drops_grants_nothing(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        run(LocalEngine(store).write([
            Tuple("organization:acme", "org", "workspace:w"), Tuple("workspace:w", "workspace", "post:p"),
            Tuple("user:carol", "member", "organization:agency"),
            Tuple("organization:agency#member", "editor", "workspace:w"),   # valid under the packaged model
        ]))
        tighter = packaged()
        for td in tighter["type_definitions"]:
            if td["type"] == "workspace":   # the next version: editors are named users only, no team grants
                td["metadata"]["relations"]["editor"]["directly_related_user_types"] = [{"type": "user"}]
        engine = LocalEngine(store, Model(tighter))
        self.assertFalse(run(engine.check("user:carol", "can_edit", Ref("post", "p"))))
        self.assertFalse(run(engine.check("organization:agency#member", "editor", Ref("workspace", "w"))))
        self.assertEqual(run(engine.list_objects("user:carol", "can_view", "post")), [])

    def test_a_tupleset_tuple_the_new_model_drops_grants_nothing(self):
        def model(parent_type: str) -> Model:
            viewer = {"relations": {"viewer": {"this": {}}},
                      "metadata": {"relations": {"viewer": {"directly_related_user_types": [{"type": "user"}]}}}}
            doc = {"type": "doc", "relations": {
                "parent": {"this": {}},
                "viewer": {"tupleToUserset": {"tupleset": {"relation": "parent"}, "computedUserset": {"relation": "viewer"}}}},
                "metadata": {"relations": {"parent": {"directly_related_user_types": [{"type": parent_type}]}}}}
            return Model({"schema_version": "1.1", "type_definitions": [
                {"type": "user"}, {"type": "folder", **viewer}, {"type": "team", **viewer}, doc]})

        tuples = Memory([("user:u", "viewer", "folder:f"), ("folder:f", "parent", "doc:d")])
        self.assertTrue(Evaluator(model("folder"), tuples).check("user:u", "viewer", "doc:d"))
        # The next version moves documents under teams; the old folder link no longer counts.
        self.assertFalse(Evaluator(model("team"), tuples).check("user:u", "viewer", "doc:d"))


def fga(handler) -> OpenFgaEngine:
    return OpenFgaEngine("http://fga.test", "s1", "m1", "k", httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


class BatchCheckAnswers(unittest.TestCase):
    """``check`` refuses an ``allowed`` that is not a JSON boolean (``{"allowed": "yes"}`` is an EngineError);
    ``batch_check`` must too, rather than read ``"false"`` as allowed."""

    def test_a_non_boolean_allowed_is_an_engine_error_never_an_allow(self):
        for allowed in ("false", "no", 1, [0], None):
            def answer(request, allowed=allowed):
                return httpx2.Response(200, json={"result": {"0": {"allowed": allowed}}})

            with self.subTest(allowed=allowed), self.assertRaises(EngineError):
                run(fga(answer).batch_check([("user:a", "can_publish", Ref("channel", "1"))]))


class LocalStoreFailures(unittest.TestCase):
    """"Any failure to answer raises EngineError" (libs/authz/engine.py): write() and read() wrap a database
    failure, but check, batch_check and list_objects let the raw sqlite3 error out. Instrumented then neither counts
    it (``result="error"``, which InkcastAuthzErrors pages on) nor logs ``authz.error``, and require() (which maps
    EngineError to 503) can't catch it."""

    def setUp(self):
        self.metrics = Metrics()
        self.metrics.counter("inkcast_authz_checks_total", "")
        self.metrics.histogram("inkcast_authz_duration_seconds", "")
        store = Store(":memory:", "k" * 40)
        self.engine = Instrumented(LocalEngine(store), self.metrics)
        store.close()   # the database is gone

    def test_check_batch_check_and_list_objects_raise_engine_error_and_are_counted(self):
        post = Ref("post", "1")
        for name, call in (("check", lambda: self.engine.check("user:a", "can_view", post)),
                           ("batch_check", lambda: self.engine.batch_check([("user:a", "can_view", post)])),
                           ("list_objects", lambda: self.engine.list_objects("user:a", "can_view", "post"))):
            with self.subTest(call=name):
                with self.assertLogs("inkcast.authz", "ERROR") as logs, self.assertRaises(EngineError):
                    run(call())
                self.assertEqual(logs.records[0].__dict__["event"], "authz.error")
        errors = self.metrics.snapshot()["inkcast_authz_checks_total"][(("relation", "can_view"), ("result", "error"))]
        self.assertEqual(errors, 3)


class IdentifierCharacters(unittest.TestCase):
    """OpenFGA refuses control characters anywhere in a type, id, relation or user (pkg/tuple/tuple.go:
    ``unicode.IsControl`` in IsValidObject, IsValidRelation, IsValidUserID, IsValidUserset). Python's ``$`` also
    matches before a final newline, so ``"post-1\\n"`` passed the "no whitespace" rule. An id OpenFGA refuses is a
    400 there, which Instrumented counts as an engine error: anyone who can put ``%0A`` or ``%00`` in a path would
    page InkcastAuthzErrors once routes build Refs (E3.F3.7), and the local engine would store tuples that can't
    be exported to OpenFGA. In text logs they also forge or rewrite authz.denied lines."""

    BAD = ("post-1\n", "a\x00b", "a\x1b[2Kb", "a\x7fb", "a\x9bb")

    def test_refs_refuse_control_characters_and_a_trailing_newline(self):
        for bad in self.BAD:
            with self.subTest(id=bad), self.assertRaises(ValueError):
                Ref("post", bad)
        with self.assertRaises(ValueError):
            Ref("post\n", "1")
        with self.assertRaises(ValueError):
            Ref.parse("post:1\n")

    def test_tuples_and_users_refuse_them_too(self):
        for bad in self.BAD:
            with self.subTest(user=bad), self.assertRaises(ValueError):
                parse_user(f"user:{bad}")
        with self.assertRaises(ValueError):
            parse_user("organization:acme#member\n")
        with self.assertRaises(ValueError):
            Tuple("user:alice", "viewer\n", "workspace:w")
        with self.assertRaises(ValueError):
            Tuple("user:alice", "viewer", "workspace:w\n")


class IdentifierLengths(unittest.TestCase):
    """OpenFGA's TupleKey (openfga/api openfga/v1/openfga.proto): ``object`` matches ``^[^\\s]{2,256}$`` (the whole
    ``type:id``), ``user`` has ``max_bytes: 512``. types.py says the same ("an object (type:id) is at most 256
    characters") but checked the type and the id separately, so a 266-character object passed. Over OpenFGA that
    is a 400 (counted as an engine error); the local engine stores it."""

    def test_an_object_is_at_most_256_characters_in_all(self):
        self.assertEqual(len(str(Ref("workspace", "x" * 246))), 256)
        with self.assertRaises(ValueError):
            Ref("workspace", "x" * 247)
        with self.assertRaises(ValueError):
            Tuple("user:a", "viewer", "workspace:" + "x" * 256)

    def test_a_user_is_at_most_512_bytes(self):
        parse_user("user:" + "x" * 256)
        with self.assertRaises(ValueError):
            parse_user("user:" + "é" * 254)   # 259 characters, 513 bytes


class DifferenceAndCycles(unittest.TestCase):
    """``viewer but not blocked``: OpenFGA's exclusion (internal/graph/check.go) answers false when the
    subtracted branch detects a cycle (``if res.resp.GetCycleDetected() || res.resp.GetAllowed()``). The evaluator
    turned a cycle into a plain False, so ``not blocked`` became True and a user OpenFGA denies was allowed."""

    def test_a_cycle_while_resolving_the_subtraction_denies(self):
        group = {"type": "group", "relations": {"member": {"this": {}}},
                 "metadata": {"relations": {"member": {"directly_related_user_types": [
                     {"type": "user"}, {"type": "group", "relation": "member"}]}}}}
        doc = {"type": "doc", "relations": {
            "viewer": {"this": {}}, "blocked": {"this": {}},
            "can_view": {"difference": {"base": {"computedUserset": {"relation": "viewer"}},
                                        "subtract": {"computedUserset": {"relation": "blocked"}}}}},
            "metadata": {"relations": {
                "viewer": {"directly_related_user_types": [{"type": "user"}]},
                "blocked": {"directly_related_user_types": [{"type": "user"}, {"type": "group", "relation": "member"}]}}}}
        model = Model({"schema_version": "1.1", "type_definitions": [{"type": "user"}, group, doc]})
        tuples = [("user:u", "viewer", "doc:1"), ("user:v", "viewer", "doc:2"),
                  ("group:a#member", "blocked", "doc:1"),
                  ("group:b#member", "member", "group:a"), ("group:a#member", "member", "group:b")]
        ev = Evaluator(model, Memory(tuples))
        self.assertFalse(ev.check("user:u", "can_view", "doc:1"))   # the blocked group's membership loops
        self.assertTrue(ev.check("user:v", "can_view", "doc:2"))    # no cycle: the subtraction still works
        self.assertEqual(ev.list_objects("user:u", "can_view", "doc"), [])


class MalformedOpenFgaAnswers(unittest.TestCase):
    """A 2xx whose JSON is not the documented object (``null``, a list, a string; a field of the wrong type)
    raised AttributeError or TypeError, not EngineError: no authz.error event or error count, no 503 from
    require(), and /readyz raised instead of reporting the problem."""

    def test_a_non_object_body_is_an_engine_error_and_is_counted(self):
        for body in (b"null", b"[]", b'"allowed"', b"1"):
            engine = Instrumented(fga(lambda r, b=body: httpx2.Response(200, content=b)))
            with self.subTest(body=body):
                with self.assertLogs("inkcast.authz", "ERROR") as logs, self.assertRaises(EngineError):
                    run(engine.check("user:a", "can_view", Ref("post", "1")))
                self.assertEqual(logs.records[0].__dict__["event"], "authz.error")
                with self.assertRaises(EngineError):
                    run(engine.list_objects("user:a", "can_view", "post"))
                self.assertIsInstance(run(fga(lambda r, b=body: httpx2.Response(200, content=b)).ready()), str)

    def test_fields_of_the_wrong_type_are_engine_errors(self):
        self.assertIsInstance(run(fga(lambda r: httpx2.Response(200, json={"authorization_model": "m1"})).ready()), str)
        with self.assertRaises(EngineError):
            run(fga(lambda r: httpx2.Response(200, json={"tuples": 5})).read())


class FgaTimeoutSetting(unittest.TestCase):
    """``INKCAST_FGA_TIMEOUT <= 0`` is refused, but ``nan`` and ``inf`` passed (``nan <= 0`` is False), and httpx2
    treats both as no timeout at all: a hung OpenFGA would hold every check forever instead of failing closed with
    a 503 within the 5 s the docs promise."""

    BASE = {"INKCAST_SECRET_KEY": "k" * 48, "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 16,
            "INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_AUTHZ_ENGINE": "openfga",
            "INKCAST_FGA_API_URL": "http://fga:8080", "INKCAST_FGA_STORE_ID": "s", "INKCAST_FGA_MODEL_ID": "m"}

    def test_nan_and_infinity_are_refused(self):
        load_config({**self.BASE, "INKCAST_FGA_TIMEOUT": "2.5"}).validate()
        for value in ("nan", "inf", "-inf", "Infinity"):
            with self.subTest(timeout=value), self.assertRaisesRegex(ConfigError, "INKCAST_FGA_TIMEOUT"):
                load_config({**self.BASE, "INKCAST_FGA_TIMEOUT": value}).validate()


if __name__ == "__main__":
    unittest.main()
