# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Authorization (E3.F3.6): a pluggable ``PolicyEngine`` over the OpenFGA model in ``authz/model.fga``.

Design: docs/authorization.md §7 and §8; decisions: ADR-0007 and ADR-0009. Enforcement (``require()``) and
route wiring arrive with E3.F3.7; the tuple lifecycle with E3.F3.8. Until then nothing calls the engine, but
every process builds one (``Context.authz``) so configuration and readiness are checked from day one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import Any

from .engine import MAX_BATCH_CHECK, MAX_DEPTH, MAX_LIST_OBJECTS, Check, PolicyEngine
from .instrumented import Instrumented
from .local import LocalEngine
from .model import PACKAGED_MODEL, Evaluator, Model, TupleSource
from .openfga import OpenFgaAdmin, OpenFgaEngine, resolve_pinned_model
from .types import (
    HIGHER_CONSISTENCY, MINIMIZE_LATENCY, RELATIONS, Channel, Consistency, EngineError, InvalidRequest,
    Organization, Post, Ref, Role, Tuple, Workspace, parse_user, relations_of, user_ref,
)

log = logging.getLogger("inkcast.authz")


def pin_model(config: Any) -> Any:
    """The configuration with the OpenFGA store and model ids pinned (E3.F3.9.S6). Explicit ``INKCAST_FGA_STORE_ID``
    and ``INKCAST_FGA_MODEL_ID`` win and nothing is asked; otherwise, with ``INKCAST_FGA_STORE_NAME``, the store
    and its version equal to the packaged model are read from OpenFGA (``resolve_pinned_model``) and pinned on a
    copy. Every other outcome (no store or no equal version yet, two stores of the name, OpenFGA unreachable or
    refusing the key) is a ``ConfigError`` that names it, so the process refuses to start and, restarted, asks again:
    the pods start on their own once the ``inkcast-authz-model`` Job has written the store and the model."""
    from ..config import AUTHZ_MODEL_JOB, ConfigError
    if config.authz_engine != "openfga" or (config.fga_store_id and config.fga_model_id) or not config.fga_store_name:
        return config
    admin = OpenFgaAdmin(config.fga_api_url, config.fga_api_token, timeout=config.fga_timeout_seconds)

    async def resolve() -> tuple[str, str]:
        try:
            return await resolve_pinned_model(admin, config.fga_store_name, json.loads(PACKAGED_MODEL.read_text()),
                                              job=AUTHZ_MODEL_JOB)
        finally:
            await admin.http.aclose()   # the client this call opened closes on the loop that used it

    try:
        store_id, model_id = asyncio.run(resolve())
    except (EngineError, InvalidRequest) as exc:
        raise ConfigError(f"INKCAST_FGA_STORE_NAME={config.fga_store_name!r}: {exc}") from exc
    log.info("OpenFGA store %r is %s; model version %s is this release's packaged model and is pinned",
             config.fga_store_name, store_id, model_id,
             extra={"event": "authz.model.pinned", "store_id": store_id, "model_id": model_id})
    return dataclasses.replace(config, fga_store_id=store_id, fga_model_id=model_id)


def build_engine(config: Any, store: Any, http: Any, metrics: Any = None) -> PolicyEngine:
    """The engine ``INKCAST_AUTHZ_ENGINE`` names, instrumented (E3.F3.6.S4). ``Config.validate`` has already
    refused a bad combination; a model file that can't be read is a ``ConfigError`` here, before serving."""
    from ..config import ConfigError
    if config.authz_engine == "openfga":
        engine: PolicyEngine = OpenFgaEngine(config.fga_api_url, config.fga_store_id, config.fga_model_id,
                                             config.fga_api_token, http, timeout=config.fga_timeout_seconds)
    else:
        try:
            engine = LocalEngine(store, Model.load(config.authz_model_path or None))
        except EngineError as exc:
            raise ConfigError(str(exc)) from exc
    return Instrumented(engine, metrics)


__all__ = [
    "Channel", "Check", "Consistency", "EngineError", "Evaluator", "HIGHER_CONSISTENCY", "Instrumented",
    "InvalidRequest", "LocalEngine", "MAX_BATCH_CHECK", "MAX_DEPTH", "MAX_LIST_OBJECTS", "MINIMIZE_LATENCY", "Model",
    "OpenFgaEngine", "Organization", "PolicyEngine", "Post", "RELATIONS", "Ref", "Role", "Tuple", "TupleSource",
    "Workspace", "build_engine", "parse_user", "pin_model", "relations_of", "user_ref",
]
