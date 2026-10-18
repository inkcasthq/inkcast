# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""v0.1 entry points, kept for compatibility: ``build(config)`` and ``asgi_app(ctx)`` give the classic
single-process server (every service at "/"). New code uses ``inkcast.launcher``."""

from __future__ import annotations

from .launcher import ALL, main, resolve
from .libs.config import Config
from .libs.http.app import compose, metrics_app as _metrics_app
from .libs.runtime import Context, build_context

App = Context


def build(config: Config, transport=None) -> Context:
    return build_context(config, transport, services=ALL)


def asgi_app(ctx: Context):
    return compose(resolve("IdentityService,PublishingApi,ConnectorService,InsightsService"), ctx)


def metrics_app(ctx: Context):
    return _metrics_app(resolve("All"), ctx)


__all__ = ["App", "asgi_app", "build", "main", "metrics_app"]
