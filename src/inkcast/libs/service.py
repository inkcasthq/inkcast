# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""What a service is, and how its names are spelled.

Every backend service is a ``ServiceSpec`` in ``inkcast.services.<pkg>`` (or a third-party
package registered under the ``inkcast.services`` entry point group). One image runs any of
them; ``INKCAST_APP_NAME`` picks which, by CamelCase name:

    INKCAST_APP_NAME=PublishingApi          → full name "publishing-api"
    INKCAST_APP_NAME=IdentityService,ConnectorService
    INKCAST_APP_NAME=All                    → every built-in service in one process

The full (kebab-case) name is used for URL prefixes (INKCAST_PATH_PREFIX=auto), Kubernetes
object names, metric labels and the ``service`` field of JSON logs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

if TYPE_CHECKING:  # pragma: no cover
    from starlette.applications import Starlette
    from starlette.routing import BaseRoute

    from .runtime import Context

_CAMEL = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)*$")


def is_camel(name: str) -> bool:
    return bool(_CAMEL.match(name))


def full_name(app_name: str) -> str:
    """``PublishingApi`` → ``publishing-api``."""
    if not is_camel(app_name):
        raise ValueError(f"{app_name!r} isn't a CamelCase service name (e.g. PublishingApi).")
    return re.sub(r"(?<!^)(?=[A-Z])", "-", app_name).lower()


def app_name_of(full: str) -> str:
    """``publishing-api`` → ``PublishingApi``."""
    return "".join(part.capitalize() for part in full.split("-"))


@dataclass(frozen=True)
class ServiceSpec:
    name: str  # CamelCase, e.g. "PublishingApi"
    summary: str
    kind: Literal["http", "worker"] = "http"
    # HTTP services: routes to serve, given the shared context.
    routes: Callable[["Context"], list["BaseRoute"]] | None = None
    # Optional base app supplying middleware and a lifespan (the MCP endpoint does this).
    base: Callable[["Context"], "Starlette"] | None = None
    # Workers: long-running loops; each gets the context and a stop event.
    workers: tuple[Callable[["Context", Any], Awaitable[None]], ...] = ()
    # Public path prefixes this service answers behind the gateway (the route table).
    public_paths: tuple[str, ...] = ()
    # What Config.validate must check for this service ("login", "secret_key", "upstreams").
    needs: tuple[str, ...] = ("secret_key",)
    # Extra readiness checks: return a problem string, or None when healthy.
    ready_checks: tuple[Callable[["Context"], str | None], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not is_camel(self.name):
            raise ValueError(f"Service name {self.name!r} must be CamelCase.")

    @property
    def full_name(self) -> str:
        return full_name(self.name)
