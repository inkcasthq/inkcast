# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""IdentityService: the OAuth 2.1 authorization server, sign-in, consent and personal access tokens.

Routes
  /.well-known/oauth-authorization-server, /authorize, /token, /register, /revoke   (MCP SDK)
  /auth/login, /auth/callback, /auth/consent       browser sign-in through the identity provider
  /tokens/new                                      browser flow that shows a new token once
  /api/v1/tokens, /api/v1/tokens/{id}              REST token management (OAuth tokens only)
"""

from __future__ import annotations

import asyncio

import hmac
import time
from datetime import datetime, timezone

from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from ...libs.auth import SCOPE, SCOPES, Principal
from ...libs.contracts.models import TokenCreate, TokenCreated, TokenInfo
from ...libs.contracts.routes import ROUTES
from ...libs.http import pages
from ...libs.http.ratelimit import limited
from ...libs.http.rest import ApiError, Authed, json_body
from ...libs.runtime import Context
from ...libs.service import ServiceSpec
from .login import LoginError, make_identity_provider
from .provider import InkcastOAuthProvider

TOKEN_REQUEST = "inkcast:token-request"  # pending_auth.client_id marking a /tokens/new request, not an OAuth client


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _info(row: dict) -> TokenInfo:
    return TokenInfo(id=row["id"], name=row["name"], scopes=row["scopes"].split(), created_at=_iso(row["created_at"]),
                     expires_at=_iso(row["expires_at"]), last_used_at=_iso(row["last_used_at"]))


def _csrf_ok(data, pending: dict) -> bool:
    """The consent form's CSRF token, compared in constant time (never with ``!=``)."""
    return hmac.compare_digest(str(data.get("csrf", "")).encode(), str(pending["csrf"]).encode())


def _check_scopes(scopes: list[str]) -> list[str]:
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise ApiError(422, "invalid_scope", f"Unknown scope(s): {', '.join(unknown)}. Choose from: {', '.join(SCOPES)}.")
    return sorted(set(scopes))


def _routes(ctx: Context) -> list[Route]:
    config, store = ctx.config, ctx.store
    oauth = InkcastOAuthProvider(config, store)
    identity = make_identity_provider(config, ctx.http)
    ctx.extras["oauth"] = oauth
    authed = Authed(ctx.verifier, config.public_url)

    settings = AuthSettings(  # the same normalisation of the issuer URL as the resource server's metadata
        issuer_url=config.public_url, resource_server_url=config.mcp_url, required_scopes=[SCOPE],
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes = create_auth_routes(
        provider=oauth,
        issuer_url=settings.issuer_url,
        client_registration_options=settings.client_registration_options,
        revocation_options=settings.revocation_options,
    )
    # Anyone can register a client (RFC 7591) and each registration is a row: bound it per client address.
    routes = [Route(r.path, limited(r.endpoint, ctx.limiter, "register"), methods=sorted(r.methods or []))
              if r.path == "/register" else r for r in routes]

    # ---- browser sign-in and consent ----------------------------------------------

    async def auth_login(request: Request) -> Response:
        pending_id = request.query_params.get("pending", "")
        if not store.get_pending(pending_id):
            return pages.message("Link expired", "This sign-in link expired. Start again from your MCP client.", error=True, status=400)
        try:
            return await identity.start(request, pending_id)
        except LoginError as exc:
            return pages.message("Can't sign in", str(exc), error=True, status=502)

    async def auth_callback(request: Request) -> Response:
        try:
            pending_id, who = await identity.finish(request)
        except LoginError as exc:
            if config.login == "password" and request.method == "POST":
                form = await request.form()
                return pages.password_form(str(form.get("pending", "")), str(exc))
            return pages.message("Sign-in failed", str(exc), error=True, status=403)
        if not store.get_pending(pending_id):
            return pages.message("Link expired", "This sign-in took too long. Start again from your MCP client.", error=True, status=400)
        user = store.upsert_user(who.key, who.display, who.email, who.name)
        store.set_pending_user(pending_id, user["id"])
        return RedirectResponse(f"/auth/consent?pending={pending_id}", status_code=303)

    async def auth_consent(request: Request) -> Response:
        data = request.query_params if request.method == "GET" else await request.form()
        pending = store.get_pending(str(data.get("pending", "")))
        if not pending or not pending["user_id"]:
            return pages.message("Link expired", "This request expired. Start again from your MCP client.", error=True, status=400)
        user = store.get_user(pending["user_id"]) or {}
        if pending["client_id"] == TOKEN_REQUEST:
            return await token_consent(request, data, pending, user)
        if request.method == "GET":
            client_name, host = oauth.describe_pending(pending)
            return pages.consent(pending["id"], pending["csrf"], client_name, host, user.get("display", "?"))
        if not _csrf_ok(data, pending):
            return pages.message("Request rejected", "The form was invalid. Start again from your MCP client.", error=True, status=400)
        target = oauth.approve(pending) if data.get("decision") == "approve" else oauth.deny(pending)
        return RedirectResponse(target, status_code=303)

    # ---- personal access tokens: browser flow (the token is shown to the person, never to an AI) ----

    async def tokens_new(request: Request) -> Response:
        q = request.query_params
        try:
            req = TokenCreate(name=q.get("name", ""), scopes=q.get("scopes", "stories:sync").split(),
                              expires_in_days=int(q["days"]) if q.get("days") else 90)
            _check_scopes(req.scopes)
        except (ValidationError, ValueError, ApiError) as exc:
            return pages.message("Invalid request", getattr(exc, "message", None) or "Check the token name, scopes and days.",
                                 error=True, status=400)
        pending_id = store.create_pending(TOKEN_REQUEST, req.model_dump_json())
        try:
            return await identity.start(request, pending_id)
        except LoginError as exc:
            return pages.message("Can't sign in", str(exc), error=True, status=502)

    async def token_consent(request: Request, data, pending: dict, user: dict) -> Response:
        req = TokenCreate.model_validate_json(pending["params"])
        if request.method == "GET":
            return pages.token_confirm(pending["id"], pending["csrf"], req.name, req.scopes, req.expires_in_days,
                                       user.get("display", "?"))
        if not _csrf_ok(data, pending):
            return pages.message("Request rejected", "The form was invalid. Start again.", error=True, status=400)
        store.delete_pending(pending["id"])
        if data.get("decision") != "approve":
            return pages.message("Cancelled", "No token was created.")
        expires = time.time() + req.expires_in_days * 86400 if req.expires_in_days else None
        token, _ = store.create_api_token(pending["user_id"], req.name, _check_scopes(req.scopes), expires)
        return pages.token_created(token, req.name)

    # ---- personal access tokens: REST (OAuth principals only; a PAT can't mint PATs) ----

    async def list_tokens(request: Request, who: Principal):
        return [_info(r) for r in store.list_api_tokens(who.user_id)]

    async def create_token(request: Request, who: Principal):
        req = TokenCreate.model_validate(await json_body(request))
        expires = time.time() + req.expires_in_days * 86400 if req.expires_in_days else None
        token, row = store.create_api_token(who.user_id, req.name, _check_scopes(req.scopes), expires)
        return TokenCreated(**_info(row).model_dump(), token=token)

    async def revoke(request: Request, who: Principal):
        if not store.revoke_api_token(who.user_id, request.path_params["token_id"]):
            raise ApiError(404, "not_found", "No such active token.")
        return {"revoked": True}

    return [
        *routes,
        Route("/auth/login", auth_login, methods=["GET"]),
        # POST is the password form: a guess per request, so it is rate limited per client address.
        Route("/auth/callback", limited(auth_callback, ctx.limiter, "login"), methods=["GET", "POST"]),
        Route("/auth/consent", auth_consent, methods=["GET", "POST"]),
        Route("/tokens/new", limited(tokens_new, ctx.limiter, "tokens-new", {"GET"}), methods=["GET"]),  # a GET that writes
        Route("/api/v1/tokens", authed(None, list_tokens, oauth_only=True), methods=["GET"]),
        Route("/api/v1/tokens", authed(None, create_token, oauth_only=True), methods=["POST"]),
        Route("/api/v1/tokens/{token_id}", authed(None, revoke, oauth_only=True), methods=["DELETE"]),
    ]


async def run_keycloak_sync(ctx: "Context", stop: asyncio.Event) -> None:
    """E9.F9.5: cut off people Keycloak disabled or deleted (services/identity/keycloak_sync.py). Off unless
    INKCAST_KEYCLOAK_SYNC_CLIENT_ID is set."""
    if not ctx.config.keycloak_sync_client_id:
        await stop.wait()
        return
    from .keycloak_sync import KeycloakSync
    sync = ctx.extras.setdefault("keycloak_sync", KeycloakSync(ctx.config, ctx.store, ctx.http, ctx.metrics))
    await sync.run_forever(stop)


SERVICE = ServiceSpec(
    name="IdentityService",
    summary="OAuth 2.1 authorization server, sign-in via Keycloak/GitHub/password, consent, personal access tokens",
    routes=_routes,
    workers=(run_keycloak_sync,),
    public_paths=ROUTES["IdentityService"],
    needs=("login", "secret_key", "authz"),
)

__all__ = ["SERVICE", "TOKEN_REQUEST"]
