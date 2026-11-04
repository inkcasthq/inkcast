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
from ...libs.http.ratelimit import limited
from ...libs.platforms import PlatformError
from ...libs.runtime import Context
from ...libs.service import ServiceSpec


def _routes(ctx: Context) -> list[Route]:
    store, registry = ctx.store, ctx.platforms

    async def connect(request: Request) -> Response:
        ticket = request.path_params["ticket"]

        def expired() -> Response:
            return pages.message("Link expired", "This connect link expired or was used. Ask Claude for a new one.",
                                 error=True, status=400)

        row = store.get_ticket(ticket)
        if not row or row["platform"] not in registry:
            return expired()
        p = registry[row["platform"]]
        if request.method == "GET":
            return pages.connect_form(ticket, p.name, p.connect_help, p.credential_fields)
        form = await request.form()
        creds = {f.key: str(form.get(f.key, "")).strip() for f in p.credential_fields}
        try:
            account = await p.verify(creds)
        except PlatformError as exc:                 # the ticket stays good: the form again, with the reason
            return pages.connect_form(ticket, p.name, p.connect_help, p.credential_fields, error=str(exc))
        # Used up now, by one conditional statement, before anything is saved: of two POSTs with the same ticket that
        # both verified, the second finds nothing and saves nothing (issue #399).
        if not store.consume_ticket(ticket):
            return expired()
        store.save_credentials(row["user_id"], p.id, creds, account.to_dict())
        who = account.handle or account.name
        return pages.message(f"{p.name} connected", f"Connected as {who}. You can close this tab and go back to Claude.")

    # Each POST verifies credentials against the platform: rate limited per client address.
    return [Route("/connect/{ticket}", limited(connect, ctx.limiter, "connect"), methods=["GET", "POST"])]


SERVICE = ServiceSpec(
    name="ConnectorService",
    summary="One-time connect pages and the encrypted platform-credential vault",
    routes=_routes,
    public_paths=ROUTES["ConnectorService"],
)
