# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of authorization (E3.F3.6): each test failed on the code it was written against and
names the behaviour it pins. OpenFGA references are to its v1.22.0 sources."""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx2

from inkcast.libs.authz import EngineError, Evaluator, Instrumented, LocalEngine, Model, OpenFgaEngine, Ref, Tuple
from inkcast.libs.authz.model import PACKAGED_MODEL
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


if __name__ == "__main__":
    unittest.main()
