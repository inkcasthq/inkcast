# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""ConnectorService: the one-time "connect a platform" pages and the credential vault behind them.

Credentials only ever arrive here, typed by the person into /connect/<ticket>; they are verified
against the platform, sealed (encrypted) and stored. No API, MCP tool or log ever returns them.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ...libs.contracts.routes import ROUTES
from ...libs.http import pages
from ...libs.platforms import PlatformError
from ...libs.runtime import Context
from ...libs.service import ServiceSpec


def _routes(ctx: Context) -> list[Route]:
    store, registry = ctx.store, ctx.platforms

    async def connect(request: Request) -> Response:
        ticket = request.path_params["ticket"]
        row = store.get_ticket(ticket)
        if not row or row["platform"] not in registry:
            return pages.message("Link expired", "This connect link expired or was used. Ask Claude for a new one.",
                                 error=True, status=400)
        p = registry[row["platform"]]
        if request.method == "GET":
            return pages.connect_form(ticket, p.name, p.connect_help, p.credential_fields)
        form = await request.form()
        creds = {f.key: str(form.get(f.key, "")).strip() for f in p.credential_fields}
        try:
            account = await p.verify(creds)
        except PlatformError as exc:
            return pages.connect_form(ticket, p.name, p.connect_help, p.credential_fields, error=str(exc))
        store.save_credentials(row["user_id"], p.id, creds, account.to_dict())
        store.consume_ticket(ticket)
        who = account.handle or account.name
        return pages.message(f"{p.name} connected", f"Connected as {who}. You can close this tab and go back to Claude.")

    return [Route("/connect/{ticket}", connect, methods=["GET", "POST"])]


SERVICE = ServiceSpec(
    name="ConnectorService",
    summary="One-time connect pages and the encrypted platform-credential vault",
    routes=_routes,
    public_paths=ROUTES["ConnectorService"],
)
