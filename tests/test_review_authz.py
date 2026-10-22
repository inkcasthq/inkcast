# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of authorization (E3.F3.6): each test failed on the code it was written against and
names the behaviour it pins. OpenFGA references are to its v1.22.0 sources."""

from __future__ import annotations

import asyncio
import json
import unittest

from inkcast.libs.authz import Evaluator, LocalEngine, Model, Ref, Tuple
from inkcast.libs.authz.model import PACKAGED_MODEL
from inkcast.libs.db import Store

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


if __name__ == "__main__":
    unittest.main()
