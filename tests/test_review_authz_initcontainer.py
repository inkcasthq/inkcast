# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #317 (E3.F3.9.S6, issue #315): the init container authz-model and its convergent store creation.

The fake OpenFGA here orders a create's three steps as OpenFGA v1.22.0 does: the id is made when the request is
handled (commands/create_store.go, ``ulid.Make()``), the row lands when the INSERT commits (postgres.go CreateStore),
and the answer follows. Two concurrent creates commit on their own connections, so an older id can land after a
younger one, and an answer can be lost after its row has landed."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time

from fakes import Fakes
from starlette.requests import Request
from starlette.responses import JSONResponse
from test_authz import free_port, serve
from test_authz_bootstrap import FakeOpenFga

from inkcast.libs.authz import pin_model

SLOW = {"INKCAST_FGA_TIMEOUT": "30"}   # a create held by the test must not time out on the writer's side


class ReorderingFakes(Fakes):
    """``create_plan``: one step per create, in arrival order: ``("land-late", event)`` makes the id, then waits for
    the event before the row lands; ``("answer-late", event)`` lands the row, then waits before answering.
    ``lose_younger_answer``: of the racers' creates (``Fakes.fga_racers``), the younger lands and answers 503.
    ``delete_fails``: that many DELETEs answer 503 and delete nothing."""

    def __init__(self, base: str):
        super().__init__(base)
        self.create_plan: list[tuple[str, threading.Event]] = []
        self.minted: list[str] = []
        self.lose_younger_answer = False
        self.delete_fails = 0

    async def fga_stores_delete(self, request: Request):
        if not self.delete_fails:
            return await super().fga_stores_delete(request)
        self.delete_fails -= 1
        self.fga_calls.append((request.method, request.url.path, None))
        return JSONResponse({"code": "internal_error", "message": "the delete did not land"}, status_code=503)

    async def fga_stores_create(self, request: Request):
        if not self.create_plan:
            response = await super().fga_stores_create(request)
            # Every racer's row has landed before any racer is answered, so the largest id is the younger's.
            younger = response.status_code == 201 and json.loads(response.body)["id"] == max(self.fga_stores)
            if self.lose_younger_answer and younger:
                self.lose_younger_answer = False
                return JSONResponse({"code": "internal_error", "message": "the store landed; the answer did not"},
                                    status_code=503)
            return response
        step, event = self.create_plan.pop(0)
        body, refused = await self._fga_begin(request)
        if refused:
            return refused
        store_id = self._fga_new_id()
        self.minted.append(store_id)
        if step == "land-late":
            await self._fga_until(event.is_set, seconds=20)
        self.fga_stores[store_id] = body["name"]
        self.fga_models[store_id] = []
        if step == "answer-late":
            await self._fga_until(event.is_set, seconds=20)
        return JSONResponse({"id": store_id, "name": body["name"]}, status_code=201)


class Reordered(FakeOpenFga):
    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.fakes = ReorderingFakes(f"http://127.0.0.1:{port}")
        cls.server = serve(cls.fakes.app(), port)

    def setUp(self):
        super().setUp()
        self.fakes.create_plan, self.fakes.minted = [], []
        self.fakes.lose_younger_answer, self.fakes.delete_fails = False, 0

    def until(self, done, what: str) -> None:
        deadline = time.monotonic() + 10
        while not done():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.01)

    def in_thread(self, name: str) -> tuple[threading.Thread, dict]:
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(zip(("code", "out", "err"), self.writer(**SLOW))), name=name)
        thread.start()
        return thread, result


class GhostStore(Reordered):
    """F1: a writer deletes its own younger store even after another writer has written the model into it and that
    writer's server has pinned it. OpenFGA's delete is soft (postgres.go DeleteStore sets deleted_at; only GetStore
    and ListStores read it), so that server goes on checking and writing tuples in a store no one else sees."""

    def test_a_store_a_server_pinned_is_never_deleted(self):
        land_a, answer_b = threading.Event(), threading.Event()
        self.fakes.create_plan = [("land-late", land_a), ("answer-late", answer_b)]
        a_thread, a = self.in_thread("A")      # lists none; its id, the older, is made, its row not yet landed
        self.until(lambda: len(self.fakes.minted) == 1, "A's create was not handled")
        b_thread, b = self.in_thread("B")      # lists none (A's row has not landed); its row lands, its answer waits
        self.until(lambda: len(self.fakes.fga_stores) == 1, "B's store did not land")
        younger = next(iter(self.fakes.fga_stores))
        c_store, c_model = self.job(**SLOW)    # C lists B's store alone, writes the model into it
        self.assertEqual(c_store, younger)
        pinned = pin_model(self.config())      # and C's server pins it: the only store of the name
        self.assertEqual((pinned.fga_store_id, pinned.fga_model_id), (c_store, c_model))
        land_a.set()                           # A's older row lands; A lists both and keeps its own, the oldest
        a_thread.join(30)
        answer_b.set()                         # B lists both: its own store is not the oldest
        b_thread.join(30)
        self.assertEqual((a.get("code"), b.get("code")), (0, 0), (a.get("err"), b.get("err")))
        self.assertIn(pinned.fga_store_id, self.fakes.fga_stores,
                      "a store a running server pinned, holding a version, must never be deleted")
        message = self.refusal(self.config())  # two stores holding a version: the next start-up names both
        self.assertIn("2 stores named 'inkcast'", message)
        self.assertIn(younger, message)


class Orphans(Reordered):
    """F2: two pods racing on a fresh install, one of whose calls is lost: the younger racer's DELETE of its own
    store, or the younger racer's create answer (its row landed). That writer exits 1 and the kubelet starts it
    again; the run that follows creates nothing, finds two stores, and on the PR head deletes neither (it deletes only
    a store it knows it created), so every server start refuses two stores until a person deletes one. The younger
    store holds no version (nobody wrote into it), hence no tuple, and no server pins it."""

    def race(self, racers: int) -> list[tuple[int, str, str]]:
        self.fakes.fga_racers = racers
        results: list = [None] * racers
        threads = [threading.Thread(target=lambda i=i: results.__setitem__(i, self.writer()), name=f"racer-{i}")
                   for i in range(racers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([m for m, p, _ in self.fakes.fga_calls if p == "/fga/stores"].count("POST"), racers)
        return results

    def assert_heals(self, results: list[tuple[int, str, str]]) -> str:
        """The racer that failed is started again by the kubelet: one store is left and the server pins it."""
        (failed,) = [err for code, _, err in results if code != 0]
        (ok,) = [out for code, out, _ in results if code == 0]
        oldest = self.ids(ok)[0]
        self.fakes.fga_races_reset()
        store_id, model_id = self.job()
        self.assertEqual(store_id, oldest)
        self.assertEqual(self.fakes.fga_stores, {oldest: "inkcast"}, "the younger store, holding no version, is gone")
        pinned = pin_model(self.config())
        self.assertEqual((pinned.fga_store_id, pinned.fga_model_id), (store_id, model_id))
        return failed

    def test_a_lost_delete_is_finished_by_the_next_run(self):
        self.fakes.delete_fails = 1
        self.assertIn("DELETE stores/", self.assert_heals(self.race(2)))

    def test_a_younger_store_whose_create_answer_was_lost_is_deleted_by_the_next_run(self):
        self.fakes.lose_younger_answer = True
        self.assertIn("POST stores", self.assert_heals(self.race(2)))



class FileSettings(FakeOpenFga):
    """K4: start-up reads INKCAST_FGA_STORE_ID, _MODEL_ID and _STORE_NAME through ``NAME_FILE`` first
    (libs/config ``_Env.get``), so the writer must too. Otherwise ids pinned through files make it create a store and
    write a version no server uses, and a name given through a file makes it write into the default store."""

    def file(self, value: str) -> str:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "value")
        with open(path, "w", encoding="utf-8") as f:
            f.write(value + "\n")
        return path

    def test_ids_pinned_through_files_mean_nothing_to_do(self):
        code, out, err = self.writer(INKCAST_FGA_STORE_ID_FILE=self.file("01JSTORE0000000000000000AA"),
                                     INKCAST_FGA_MODEL_ID_FILE=self.file("01JMODEL0000000000000000AA"))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "INKCAST_FGA_STORE_ID=01JSTORE0000000000000000AA\nINKCAST_FGA_MODEL_ID=01JMODEL0000000000000000AA\n")
        self.assertEqual(self.fakes.fga_calls, [], "no call at all")

    def test_the_store_name_given_through_a_file_is_the_one_written(self):
        env = {"INKCAST_FGA_STORE_NAME": "", "INKCAST_FGA_STORE_NAME_FILE": self.file("inkcast-staging")}
        store_id, model_id = self.job(**env)
        self.assertEqual(self.fakes.fga_stores, {store_id: "inkcast-staging"})
        pinned = pin_model(self.config(**env))
        self.assertEqual((pinned.fga_store_id, pinned.fga_model_id), (store_id, model_id))

    def test_an_unreadable_file_is_named_and_nothing_is_called(self):
        code, out, err = self.writer(INKCAST_FGA_STORE_ID_FILE="/nonexistent/store-id")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("INKCAST_FGA_STORE_ID_FILE", err)
        self.assertEqual(self.fakes.fga_calls, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
