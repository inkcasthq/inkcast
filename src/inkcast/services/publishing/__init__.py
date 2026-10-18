# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""PublishingApi: posts, previews, Git publishing (stories sync + ledger), the REST API and /mcp."""

from __future__ import annotations

from ...libs.contracts.routes import ROUTES
from ...libs.http.rest import Authed
from ...libs.runtime import Context
from ...libs.service import ServiceSpec
from .mcp_tools import build_mcp, mcp_app
from .rest import build_routes
from .sync import GitSync
from .usecases import InkcastService


def _parts(ctx: Context):
    if "publishing" not in ctx.extras:
        service = InkcastService(ctx.store, ctx.platforms, ctx.publisher, ctx.config.public_url)
        sync = GitSync(ctx.store, ctx.platforms, ctx.publisher, ctx.metrics)
        ctx.extras["publishing"] = (service, sync)
    return ctx.extras["publishing"]


def _base(ctx: Context):
    service, sync = _parts(ctx)
    mcp = build_mcp(ctx, service, sync)
    ctx.extras["mcp"] = mcp
    return mcp_app(ctx, mcp)


def _routes(ctx: Context):
    service, sync = _parts(ctx)
    return build_routes(service, sync, Authed(ctx.verifier, ctx.config.public_url), ctx.config.public_url)


SERVICE = ServiceSpec(
    name="PublishingApi",
    summary="REST /api/v1, MCP /mcp, previews, Git publishing (sync + ledger)",
    base=_base,
    routes=_routes,
    public_paths=ROUTES["PublishingApi"],
)
