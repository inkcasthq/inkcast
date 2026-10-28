# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #285 (issue #279: the OpenFGA component and ``inkcast authz write-model``). Each test
failed on the PR head (39466377) and passes with the fix committed with it."""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest

from fakes import Fakes
from test_authz import free_port, serve

from inkcast.authz_cli import authz_main
from inkcast.libs.authz.model import PACKAGED_MODEL
from inkcast.libs.authz.openfga import same_model


def doc_model(refs: list[dict], relation: bool = True) -> dict:
    """A compiled model whose ``doc#viewer`` is assignable by ``refs``; without ``relation`` the metadata stays but
    ``viewer`` is no relation (OpenFGA v1.22.0 accepts that model, and answers a Check on viewer with a 400)."""
    doc = {"type": "doc", "metadata": {"relations": {"viewer": {"directly_related_user_types": refs}}}}
    if relation:
        doc["relations"] = {"viewer": {"this": {}}}
    return {"schema_version": "1.1", "type_definitions": [{"type": "user"}, doc]}


DIRECT = doc_model([{"type": "user"}])                       # define viewer: [user]
PUBLIC = doc_model([{"type": "user", "wildcard": {}}])       # define viewer: [user:*]
NO_RELATION = doc_model([{"type": "user"}], relation=False)


def stored(model_id: str, refs: list[dict], relation: bool = True) -> dict:
    """What OpenFGA v1.22.0 answers for those models (GET /stores/{store}/authorization-models/{id}, observed
    2026-10-28 against openfga/openfga:v1.22.0@sha256:9cf9a20a...)."""
    return {"id": model_id, "schema_version": "1.1", "type_definitions": [
        {"type": "user", "relations": {}, "metadata": None},
        {"type": "doc", "relations": {"viewer": {"this": {}}} if relation else {},
         "metadata": {"relations": {"viewer": {"directly_related_user_types": refs, "module": "", "source_info": None}},
                      "module": "", "source_info": None}}], "conditions": {}}


STORED_DIRECT = stored("01M60HN8MCFCN60FG9PCDH3VWY", [{"type": "user", "condition": ""}])
STORED_PUBLIC = stored("01M60HN8MNA7X5X6EMNK5Y58QK", [{"type": "user", "wildcard": {}, "condition": ""}])
STORED_NO_RELATION = stored("01M60HN8MW47HECMRECNM841EJ", [{"type": "user", "condition": ""}], relation=False)


class SameModelKeepsWhatAnEmptyMessageMeans(unittest.TestCase):
    """``wildcard: {}`` (``type:*``) and ``this: {}`` (a directly assignable relation) are empty protobuf messages
    whose presence is the meaning: dropping them as empty values makes different models compare equal, so
    write-model reports "unchanged" and prints the id of a version that is not the file."""

    def test_the_server_s_answer_is_still_the_file(self):
        self.assertTrue(same_model(STORED_DIRECT, DIRECT))
        self.assertTrue(same_model(STORED_PUBLIC, PUBLIC))
        self.assertTrue(same_model(STORED_NO_RELATION, NO_RELATION))

    def test_user_and_every_user_are_different_models(self):
        self.assertFalse(same_model(STORED_PUBLIC, DIRECT), "user:* stored, [user] in the file: public access stays")
        self.assertFalse(same_model(STORED_DIRECT, PUBLIC), "[user] stored, user:* in the file: never written")

    def test_a_directly_assignable_relation_is_not_its_absence(self):
        self.assertFalse(same_model(STORED_NO_RELATION, DIRECT))
        self.assertFalse(same_model(STORED_DIRECT, NO_RELATION))


class WriteModelRevokesAPublicGrant(unittest.TestCase):
    """End to end against the fake OpenFGA: the store's newest version lets ``user:*`` be an organization's
    billing_admin; the packaged model allows named users only. write-model must write the packaged model and print
    its id, not report the public version as unchanged."""

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

    def run_cli(self, *args: str) -> tuple[int, dict[str, str], str]:
        out, err = io.StringIO(), io.StringIO()
        code = authz_main(["write-model", *args], environ={"INKCAST_FGA_API_URL": f"{self.fakes.base}/fga",
                                                           "INKCAST_FGA_API_TOKEN": self.fakes.fga_token}, out=out, err=err)
        return code, dict(line.split("=", 1) for line in out.getvalue().splitlines()), err.getvalue()

    def test_removing_user_wildcard_writes_a_new_version(self):
        public = json.loads(PACKAGED_MODEL.read_text())
        org = next(td for td in public["type_definitions"] if td["type"] == "organization")
        org["metadata"]["relations"]["billing_admin"]["directly_related_user_types"] = [{"type": "user", "wildcard": {}}]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(public, f)
        self.addCleanup(pathlib.Path(f.name).unlink)
        code, first, err = self.run_cli("--model", f.name)
        self.assertEqual(code, 0, err)
        code, second, err = self.run_cli()
        self.assertEqual(code, 0, err)
        self.assertEqual(second["INKCAST_FGA_STORE_ID"], first["INKCAST_FGA_STORE_ID"])
        self.assertNotEqual(second["INKCAST_FGA_MODEL_ID"], first["INKCAST_FGA_MODEL_ID"], err)
        self.assertIn("model written", err)
        self.assertEqual(len(self.fakes.fga_models[first["INKCAST_FGA_STORE_ID"]]), 2)


if __name__ == "__main__":
    unittest.main()
