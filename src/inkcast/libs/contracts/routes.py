# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The public route table: which service answers which path behind the gateway.

The built-in Gateway routes with it, ``deploy/kustomize/components/gateway-api`` is checked
against it by tests, and each ``ServiceSpec.public_paths`` must agree with it. Paths are matched
by prefix, longest first; a path ending in ``/`` matches everything below it, any other path
matches itself exactly plus anything below it.

Clients only ever see these stable paths; how services are split behind them can change freely.
"""

from __future__ import annotations

ROUTES: dict[str, tuple[str, ...]] = {
    "IdentityService": (
        "/.well-known/oauth-authorization-server",
        "/authorize", "/token", "/register", "/revoke",
        "/auth/",
        "/api/v1/tokens",
        "/api/v1/account",
        "/tokens/",
        "/account/",
    ),
    "PublishingApi": (
        "/mcp",
        "/.well-known/oauth-protected-resource",
        "/api",
    ),
    "ConnectorService": (
        "/connect/",
    ),
    "InsightsService": (
        "/api/v1/insights",  # longer than PublishingApi's /api, so it wins (longest prefix first)
    ),
}


def table() -> list[tuple[str, str]]:
    """(path prefix, service) pairs, longest prefix first."""
    pairs = [(path, service) for service, paths in ROUTES.items() for path in paths]
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def match(path: str) -> str | None:
    for prefix, service in table():
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return service
        elif path == prefix or path.startswith(prefix + "/"):
            return service
    return None
