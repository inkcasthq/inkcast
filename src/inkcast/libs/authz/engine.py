# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The ``PolicyEngine`` Protocol (E3.F3.6.S1): what every engine answers, and nothing else.

Two engines implement it (``OpenFgaEngine`` over OpenFGA's REST API, ``LocalEngine`` in-process over the same
compiled model) and ``Instrumented`` wraps either with metrics and logs. Enforcement (``require()``, E3.F3.7)
and the tuple lifecycle (E3.F3.8) only ever see this interface, so an engine is swapped by configuration.

Rules shared by every engine:

* any failure to answer raises ``EngineError`` (callers fail closed; a denial is ``False``, never an exception);
* ``write`` follows OpenFGA: writing a tuple that exists, or deleting one that doesn't, is an ``InvalidRequest``,
  and a request is applied whole or not at all;
* ``list_objects`` is capped (``MAX_LIST_OBJECTS``, OpenFGA's own default), which is why list pages ask for
  workspaces and filter posts in the database (docs/authorization.md §8).
"""

from __future__ import annotations

from typing import Iterable, Protocol, Sequence, runtime_checkable

from .types import MINIMIZE_LATENCY, Consistency, Ref, Tuple

# OpenFGA's defaults: OPENFGA_LIST_OBJECTS_MAX_RESULTS and OPENFGA_MAX_CHECKS_PER_BATCH_CHECK.
MAX_LIST_OBJECTS = 1000
MAX_BATCH_CHECK = 50
# OpenFGA's resolution depth: a check that follows more than this many relations in a row is an error.
MAX_DEPTH = 25

Check = tuple[str, str, Ref]  # (user, relation, object)


@runtime_checkable
class PolicyEngine(Protocol):
    name: str  # "local" | "openfga": the metrics label

    async def check(self, user: str, relation: str, obj: Ref, *, consistency: Consistency = MINIMIZE_LATENCY) -> bool:
        """Does ``user`` (``user:<id>``, or a userset) have ``relation`` on ``obj``? Errors raise ``EngineError``."""
        ...

    async def batch_check(self, items: Sequence[Check], *, consistency: Consistency = MINIMIZE_LATENCY) -> list[bool]:
        """One answer per item, in order. Any failure fails the whole batch."""
        ...

    async def list_objects(self, user: str, relation: str, type_: str, *,
                           consistency: Consistency = MINIMIZE_LATENCY) -> list[str]:
        """Objects of ``type_`` (``"type:id"`` strings) on which ``user`` has ``relation``; at most ``MAX_LIST_OBJECTS``."""
        ...

    async def write(self, writes: Iterable[Tuple] = (), deletes: Iterable[Tuple] = ()) -> None:
        """Add and remove tuples atomically; a duplicate write or a missing delete is an ``InvalidRequest``."""
        ...

    async def read(self, *, user: str | None = None, relation: str | None = None, obj: Ref | None = None,
                   type_: str | None = None) -> list[Tuple]:
        """Stored tuples matching the filter: an object (with an optional relation), a user with an object type,
        or everything. Only for the lifecycle and the reconciler (E3.F3.8), never for a permission decision."""
        ...

    async def ready(self) -> str | None:
        """A readiness problem, or None: the store and the pinned model exist (OpenFGA), the model loaded (local).
        Cached by the engine, so /readyz never hammers the backend."""
        ...
