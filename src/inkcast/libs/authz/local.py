# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``LocalEngine`` (E3.F3.6.S3): the compiled model evaluated in-process over tuples in the Inkcast database.

For tests, laptops and small air-gapped self-hosts: no OpenFGA to run, the same answers (both engines pass
``authz/model.fga.yaml``). It is not for the hosted SaaS (the production posture check refuses it unless
``INKCAST_AUTHZ_ALLOW_LOCAL`` says this is a self-host): a Zanzibar-style store scales and audits better, and
``list_objects`` here checks every candidate object one by one.

Consistency options are accepted and ignored: a read after a write in the same SQLite database is always
consistent.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .engine import MAX_BATCH_CHECK, Check
from .model import Evaluator, Model
from .types import MINIMIZE_LATENCY, Consistency, EngineError, InvalidRequest, Ref, Tuple


class _StoreSource:
    """The ``TupleSource`` over ``AuthzRepo`` (libs/db/authz.py)."""

    def __init__(self, store: Any):
        self.store = store

    def users_of(self, obj: str, relation: str) -> list[str]:
        return self.store.authz_users_of(obj, relation)

    def objects_of_type(self, type_: str) -> list[str]:
        return self.store.authz_objects_of_type(type_)


def validate_write(model: Model, writes: list[Tuple], deletes: list[Tuple]) -> None:
    """What OpenFGA refuses before touching the store: an empty request, a tuple listed twice, one both written
    and deleted, and (for writes) a user type the model does not allow on that relation."""
    if not writes and not deletes:
        raise InvalidRequest("a write needs at least one tuple to write or delete")
    seen: set[Tuple] = set()
    for t in (*writes, *deletes):
        if t in seen:
            raise InvalidRequest(f"tuple listed twice in one write: {t.user} {t.relation} {t.object}")
        seen.add(t)
    for t in writes:
        model.validate_tuple(t)
    for t in deletes:
        Ref.parse(t.object)


class LocalEngine:
    name = "local"

    def __init__(self, store: Any, model: Model | None = None):
        self.model = model or Model.load()
        self.store = store
        self.evaluator = Evaluator(self.model, _StoreSource(store))

    async def check(self, user: str, relation: str, obj: Ref, *, consistency: Consistency = MINIMIZE_LATENCY) -> bool:
        try:
            return self.evaluator.check(user, relation, str(obj))
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc

    async def batch_check(self, items: Sequence[Check], *, consistency: Consistency = MINIMIZE_LATENCY) -> list[bool]:
        if len(items) > MAX_BATCH_CHECK:
            raise InvalidRequest(f"a batch check takes at most {MAX_BATCH_CHECK} items (got {len(items)})")
        return [await self.check(user, relation, obj, consistency=consistency) for user, relation, obj in items]

    async def list_objects(self, user: str, relation: str, type_: str, *,
                           consistency: Consistency = MINIMIZE_LATENCY) -> list[str]:
        try:
            return self.evaluator.list_objects(user, relation, type_)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc

    async def write(self, writes: Iterable[Tuple] = (), deletes: Iterable[Tuple] = ()) -> None:
        writes, deletes = list(writes), list(deletes)
        validate_write(self.model, writes, deletes)
        try:
            self.store.authz_write([(t.user, t.relation, t.object) for t in writes],
                                   [(t.user, t.relation, t.object) for t in deletes])
        except ValueError as exc:  # TupleConflict: the same refusal OpenFGA gives
            raise InvalidRequest(str(exc)) from exc
        except Exception as exc:  # the database itself: locked, gone, full
            raise EngineError(f"authorization store write failed: {type(exc).__name__}") from exc

    async def read(self, *, user: str | None = None, relation: str | None = None, obj: Ref | None = None,
                   type_: str | None = None) -> list[Tuple]:
        if relation is not None and obj is None and type_ is None:
            raise InvalidRequest("reading by relation needs an object or an object type")
        try:
            rows = self.store.authz_read(user=user, relation=relation, obj=str(obj) if obj else None, type_=type_)
        except Exception as exc:
            raise EngineError(f"authorization store read failed: {type(exc).__name__}") from exc
        return [Tuple(u, r, o) for u, r, o in rows]

    async def ready(self) -> str | None:
        return None if self.model.types else "authorization model has no types"
