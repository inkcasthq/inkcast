# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``Instrumented`` (E3.F3.6.S5): the metrics and log lines every engine gets, in one place.

Whichever engine answers, each check is counted in ``inkcast_authz_checks_total{relation,result}`` (``allowed``,
``denied`` or ``error``), every call is timed in ``inkcast_authz_duration_seconds{op,engine}``, a denial is the
``authz.denied`` event and a failure the ``authz.error`` event. The log lines carry ids only (user, relation,
object; ADR-0009 makes them opaque), never a token or a name. ``require()`` (E3.F3.7) therefore only raises.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Sequence

from .engine import Check, PolicyEngine
from .types import MINIMIZE_LATENCY, Consistency, EngineError, Ref, Tuple

log = logging.getLogger("inkcast.authz")


class Instrumented:
    def __init__(self, engine: PolicyEngine, metrics: Any = None, clock=time.perf_counter):
        self.engine, self.metrics, self.clock = engine, metrics, clock
        self.name = engine.name

    # ---- helpers --------------------------------------------------------------------------------------------

    def _count(self, relation: str, result: str) -> None:
        if self.metrics is not None:
            self.metrics.inc("inkcast_authz_checks_total", relation=relation, result=result)

    def _observe(self, op: str, started: float) -> None:
        if self.metrics is not None:
            self.metrics.observe("inkcast_authz_duration_seconds", self.clock() - started, op=op, engine=self.name)

    def _failed(self, op: str, exc: EngineError, user: str = "", relation: str = "", obj: str = "") -> None:
        log.error("authorization %s failed on the %s engine: %s", op, self.name, exc,
                  extra={"event": "authz.error", "op": op, "engine": self.name, "user": user, "relation": relation,
                         "object": obj})

    # ---- the PolicyEngine -----------------------------------------------------------------------------------

    async def check(self, user: str, relation: str, obj: Ref, *, consistency: Consistency = MINIMIZE_LATENCY) -> bool:
        started = self.clock()
        try:
            allowed = await self.engine.check(user, relation, obj, consistency=consistency)
        except EngineError as exc:
            self._count(relation, "error")
            self._observe("check", started)
            self._failed("check", exc, user, relation, str(obj))
            raise
        self._observe("check", started)
        self._count(relation, "allowed" if allowed else "denied")
        if not allowed:
            log.warning("denied: %s lacks %s on %s", user, relation, obj,
                        extra={"event": "authz.denied", "user": user, "relation": relation, "object": str(obj)})
        return allowed

    async def batch_check(self, items: Sequence[Check], *, consistency: Consistency = MINIMIZE_LATENCY) -> list[bool]:
        started = self.clock()
        try:
            answers = await self.engine.batch_check(items, consistency=consistency)
        except EngineError as exc:
            for _, relation, _ in items:
                self._count(relation, "error")
            self._observe("batch_check", started)
            self._failed("batch_check", exc, relation=",".join(sorted({r for _, r, _ in items})))
            raise
        self._observe("batch_check", started)
        for (user, relation, obj), allowed in zip(items, answers):
            self._count(relation, "allowed" if allowed else "denied")
            if not allowed:
                log.warning("denied: %s lacks %s on %s", user, relation, obj,
                            extra={"event": "authz.denied", "user": user, "relation": relation, "object": str(obj)})
        return answers

    async def list_objects(self, user: str, relation: str, type_: str, *,
                           consistency: Consistency = MINIMIZE_LATENCY) -> list[str]:
        started = self.clock()
        try:
            return await self.engine.list_objects(user, relation, type_, consistency=consistency)
        except EngineError as exc:
            self._count(relation, "error")
            self._failed("list_objects", exc, user, relation, f"{type_}:")
            raise
        finally:
            self._observe("list_objects", started)

    async def write(self, writes: Iterable[Tuple] = (), deletes: Iterable[Tuple] = ()) -> None:
        started = self.clock()
        try:
            await self.engine.write(writes, deletes)
        except EngineError as exc:
            self._failed("write", exc)
            raise
        finally:
            self._observe("write", started)

    async def read(self, *, user: str | None = None, relation: str | None = None, obj: Ref | None = None,
                   type_: str | None = None) -> list[Tuple]:
        started = self.clock()
        try:
            return await self.engine.read(user=user, relation=relation, obj=obj, type_=type_)
        except EngineError as exc:
            self._failed("read", exc, user or "", relation or "", str(obj) if obj else (f"{type_}:" if type_ else ""))
            raise
        finally:
            self._observe("read", started)

    async def ready(self) -> str | None:
        return await self.engine.ready()
