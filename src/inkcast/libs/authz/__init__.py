# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Authorization (E3.F3.6): a pluggable ``PolicyEngine`` over the OpenFGA model in ``authz/model.fga``.

Design: docs/authorization.md §7 and §8; decisions: ADR-0007 and ADR-0009. Enforcement (``require()``) and
route wiring arrive with E3.F3.7; the tuple lifecycle with E3.F3.8. Until then nothing calls the engine, but
every process builds one (``Context.authz``) so configuration and readiness are checked from day one.
"""

from __future__ import annotations

from typing import Any

from .engine import MAX_BATCH_CHECK, MAX_DEPTH, MAX_LIST_OBJECTS, Check, PolicyEngine
from .instrumented import Instrumented
from .local import LocalEngine
from .model import Evaluator, Model, TupleSource
from .openfga import OpenFgaEngine
from .types import (
    HIGHER_CONSISTENCY, MINIMIZE_LATENCY, RELATIONS, Channel, Consistency, EngineError, InvalidRequest,
    Organization, Post, Ref, Role, Tuple, Workspace, parse_user, relations_of, user_ref,
)



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
    "Workspace", "build_engine", "parse_user", "relations_of", "user_ref",
]
