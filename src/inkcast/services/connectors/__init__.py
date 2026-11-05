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
from ...libs.http.onetime import PageTokens
from ...libs.http.ratelimit import limited
from ...libs.platforms import PlatformError
from ...libs.runtime import Context
from ...libs.service import ServiceSpec


def _routes(ctx: Context) -> list[Route]:
    store, registry = ctx.store, ctx.platforms
    tokens = PageTokens(ctx.config.secret_key, "connect")

    async def connect(request: Request) -> Response:
        ticket = request.path_params["ticket"]

        def expired() -> Response:
            return pages.message("Link expired", "This connect link expired or was used. Ask Claude for a new one.",
                                 error=True, status=400)

        row = store.get_ticket(ticket)
        if not row or row["platform"] not in registry:
            return expired()
        p = registry[row["platform"]]
        if request.method != "POST":                 # a GET, or the HEAD a previewer sends: the form, nothing verified (#401)
            return pages.connect_form(ticket, p.name, p.connect_help, p.credential_fields, tokens.make(ticket))
        form = await request.form()
        if not tokens.ok(ticket, form.get("csrf")):  # not from the form Inkcast served for this ticket
            return pages.message("Request rejected", "The form was invalid. Open the link again.", error=True, status=400)
        creds = {f.key: str(form.get(f.key, "")).strip() for f in p.credential_fields}
        try:
            account = await p.verify(creds)
        except PlatformError as exc:                 # the ticket stays good: the form again, with the reason
            return pages.connect_form(ticket, p.name, p.connect_help, p.credential_fields, tokens.make(ticket), error=str(exc))
        # Used up now, by one conditional statement, before anything is saved: of two POSTs with the same ticket that
        # both verified, the second finds nothing and saves nothing (issue #399). One transaction with the save, so a
        # save that fails leaves the link good.
        if not store.consume_ticket_and_save(ticket, row["user_id"], p.id, creds, account.to_dict()):
            return expired()
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
