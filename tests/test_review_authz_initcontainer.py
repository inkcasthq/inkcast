# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #317 (E3.F3.9.S6, issue #315): the init container authz-model and its convergent store creation.

The fake OpenFGA here orders a create's three steps as OpenFGA v1.22.0 does: the id is made when the request is
handled (commands/create_store.go, ``ulid.Make()``), the row lands when the INSERT commits (postgres.go CreateStore),
and the answer follows. Two concurrent creates commit on their own connections, so an older id can land after a
younger one, and an answer can be lost after its row has landed."""

from __future__ import annotations

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
    the event before the row lands; ``("answer-late", event)`` lands the row, then waits before answering."""

    def __init__(self, base: str):
        super().__init__(base)
        self.create_plan: list[tuple[str, threading.Event]] = []
        self.minted: list[str] = []

    async def fga_stores_create(self, request: Request):
        if not self.create_plan:
            return await super().fga_stores_create(request)
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


if __name__ == "__main__":
    import unittest
    unittest.main()
