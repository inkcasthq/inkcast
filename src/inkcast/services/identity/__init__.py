# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""IdentityService: the OAuth 2.1 authorization server, sign-in, consent and personal access tokens.

Routes
  /.well-known/oauth-authorization-server, /authorize, /token, /register, /revoke   (MCP SDK)
  /auth/login, /auth/callback, /auth/consent       browser sign-in through the identity provider
  /tokens/new                                      browser flow that shows a new token once
  /api/v1/tokens, /api/v1/tokens/{id}              REST token management (OAuth tokens only)
  /api/v1/account/deletion-link                    REST: a one-time link to the deletion page (OAuth tokens only)
  /account/delete/{ticket}                         delete my account: the page (after a fresh sign-in)
  /account/return                                  where Keycloak's info pages link back (the client's baseUrl);
                                                   finishes a deletion when the visit carries its cookie
"""

from __future__ import annotations

import asyncio

import hmac
import time
from datetime import datetime, timezone

import httpx2
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from ...libs.account import COOKIE, RETURN_TTL, deletion_link
from ...libs.auth import SCOPE, SCOPES, Principal
from ...libs.contracts.models import TokenCreate, TokenCreated, TokenInfo
from ...libs.contracts.routes import ROUTES
from ...libs.http import pages
from ...libs.http.ratelimit import WRITE_METHODS, limited
from ...libs.http.rest import ApiError, Authed, json_body
from ...libs.runtime import Context
from ...libs.service import ServiceSpec
from .login import LoginError, make_identity_provider
from .provider import InkcastOAuthProvider

TOKEN_REQUEST = "inkcast:token-request"  # pending_auth.client_id marking a /tokens/new request, not an OAuth client
DELETE_REQUEST = "inkcast:delete-request"  # ... a /account/delete/{ticket} visit: sign in again, then the confirm page
DELETE_ACTION = "inkcast:delete-action"    # ... the round trip to Keycloak's delete_account action (cancel or error)


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _info(row: dict) -> TokenInfo:
    return TokenInfo(id=row["id"], name=row["name"], scopes=row["scopes"].split(), created_at=_iso(row["created_at"]),
                     expires_at=_iso(row["expires_at"]), last_used_at=_iso(row["last_used_at"]))


def _csrf_ok(data, pending: dict) -> bool:
    """The consent form's CSRF token, compared in constant time (never with ``!=``)."""
    return hmac.compare_digest(str(data.get("csrf", "")).encode(), str(pending["csrf"]).encode())


def _consent_cookie(identity, pending_id: str, user_id: str) -> tuple[str, str]:
    """(name, value) of the cookie that binds a pending consent to the browser that signed in, as that person: the
    pending id alone travels in URLs (it is the OIDC state), so it must not be enough to answer the consent."""
    return (f"inkcast_consent_{identity._derived('consent-cookie', pending_id)[:16]}",
            identity._derived("consent", f"{pending_id}:{user_id}"))


def _same_browser(request: Request, identity, pending: dict) -> bool:
    name, value = _consent_cookie(identity, pending["id"], pending["user_id"])
    return hmac.compare_digest(request.cookies.get(name, "").encode(), value.encode())


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
    # Anyone can register a client (RFC 7591) and each registration is a row: bound it per client address. So is each
    # /authorize request (a pending row), GET included.
    guarded = {"/register": WRITE_METHODS, "/authorize": {"GET", "POST"}}
    routes = [Route(r.path, limited(r.endpoint, ctx.limiter, r.path.strip("/"), guarded[r.path]), methods=sorted(r.methods or []))
              if r.path in guarded else r for r in routes]

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
        action = store.get_pending(request.query_params.get("state", ""))
        if action and action["client_id"] == DELETE_ACTION:
            return deletion_return(request, action)
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
        response = RedirectResponse(f"/auth/consent?pending={pending_id}", status_code=303)
        name, value = _consent_cookie(identity, pending_id, user["id"])
        response.set_cookie(name, value, max_age=900, httponly=True, samesite="lax",
                            secure=config.public_url.startswith("https://"))
        return response

    async def auth_consent(request: Request) -> Response:
        data = request.query_params if request.method == "GET" else await request.form()
        pending = store.get_pending(str(data.get("pending", "")))
        if not pending or not pending["user_id"]:
            return pages.message("Link expired", "This request expired. Start again from your MCP client.", error=True, status=400)
        if not _same_browser(request, identity, pending):
            return pages.message("Request rejected", "This request was not signed in from this browser. Start again "
                                 "from your own MCP client.", error=True, status=400)
        user = store.get_user(pending["user_id"]) or {}
        if pending["client_id"] == TOKEN_REQUEST:
            return await token_consent(request, data, pending, user)
        if pending["client_id"] == DELETE_REQUEST:
            return await deletion_consent(request, data, pending, user)
        if request.method == "GET":
            client_name, host = oauth.describe_pending(pending)
            return pages.consent(pending["id"], pending["csrf"], client_name, host, user.get("display", "?"))
        if not _csrf_ok(data, pending):
            return pages.message("Request rejected", "The form was invalid. Start again from your MCP client.", error=True, status=400)
        target = oauth.approve(pending) if data.get("decision") == "approve" else oauth.deny(pending)
        return RedirectResponse(target, status_code=303)

    # ---- personal access tokens: browser flow (the token is shown to the person, never to an AI) ----

    def _expiry(req: TokenCreate) -> int | None:
        """The token's lifetime in days (None = never) under this server's policy (F1.10.S2, issue #90): the configured
        default when the request didn't say; refused with a 422 naming the limit over INKCAST_PAT_MAX_DAYS, or never
        while INKCAST_PAT_ALLOW_NO_EXPIRY is false. Checked where the token is made, so a /tokens/new link made under
        an older, looser policy is held to the current one."""
        days = req.expires_in_days if "expires_in_days" in req.model_fields_set else config.pat.default_days
        why = config.pat.refusal(days)
        if why:
            raise ApiError(422, "invalid_expiry", why)
        return days

    async def tokens_new(request: Request) -> Response:
        q = request.query_params
        try:
            days = q.get("days", "").strip()                   # a number of days, "never", or nothing for the default
            req = TokenCreate(name=q.get("name", ""), scopes=q.get("scopes", "stories:sync").split(),
                              expires_in_days=None if days == "never" else int(days) if days else config.pat.default_days)
            _check_scopes(req.scopes)
        except (ValidationError, ValueError, ApiError) as exc:
            return pages.message("Invalid request", getattr(exc, "message", None) or "Check the token name, scopes and days.",
                                 error=True, status=400)
        try:
            _expiry(req)
        except ApiError as exc:                                # over this server's limit: a 422 naming it
            return pages.message("Invalid request", exc.message, error=True, status=exc.status)
        pending_id = store.create_pending(TOKEN_REQUEST, req.model_dump_json())
        try:
            return await identity.start(request, pending_id)
        except LoginError as exc:
            return pages.message("Can't sign in", str(exc), error=True, status=502)

    async def token_consent(request: Request, data, pending: dict, user: dict) -> Response:
        req = TokenCreate.model_validate_json(pending["params"])
        try:
            days = _expiry(req)
        except ApiError as exc:                                # the policy tightened since the link was made
            store.delete_pending(pending["id"])
            return pages.message("Token not created", exc.message, error=True, status=exc.status)
        if request.method == "GET":
            return pages.token_confirm(pending["id"], pending["csrf"], req.name, req.scopes, days, user.get("display", "?"))
        if not _csrf_ok(data, pending):
            return pages.message("Request rejected", "The form was invalid. Start again.", error=True, status=400)
        store.delete_pending(pending["id"])
        if data.get("decision") != "approve":
            return pages.message("Cancelled", "No token was created.")
        expires = time.time() + days * 86400 if days else None
        token, _ = store.create_api_token(pending["user_id"], req.name, _check_scopes(req.scopes), expires)
        return pages.token_created(token, req.name)

    # ---- delete my account (E9.F9.5.S4): Inkcast's page, then Keycloak's delete_account action -------------
    # The link (MCP inkcast_delete_account, POST /api/v1/account/deletion-link) opens a fresh sign-in; the confirm
    # page's Continue sends the browser to Keycloak with kc_action=delete_account. Keycloak asks for the password
    # again, confirms, deletes the account on the person's own authority and shows its own "deleted" page, whose
    # "Back to application" link (the client's baseUrl) is /account/return. That visit is matched to the request
    # by a cookie, confirmed through the sync's read-only view-users (the person must be gone), and only then
    # erased (via=return). Cancel and errors come back through /auth/callback with nothing deleted. The sync's
    # DELETE_ACCOUNT reader (via=event) erases anyone the return visit never confirmed.

    async def account_delete(request: Request) -> Response:
        req = store.open_deletion_link(request.path_params["ticket"])
        if not req:
            return pages.message("Link expired", "This link was already used or has expired. Ask for a new one from your "
                                 "MCP client or the API.", error=True, status=400)
        pending_id = store.create_pending(DELETE_REQUEST, req["id"])
        try:
            return await identity.start(request, pending_id)
        except LoginError as exc:
            return pages.message("Can't sign in", str(exc), error=True, status=502)

    async def deletion_consent(request: Request, data, pending: dict, user: dict) -> Response:
        req = store.get_deletion_request(pending["params"])
        if not req or req["user_id"] != pending["user_id"]:       # bound to the person who asked for the link
            store.delete_pending(pending["id"])
            return pages.message("Request rejected", "This deletion link belongs to another account, or has expired. "
                                 "Nothing was deleted.", error=True, status=403)
        if request.method == "GET":
            return pages.deletion_confirm(pending["id"], pending["csrf"], user.get("display", "?"), config.backup_keep)
        if not _csrf_ok(data, pending):
            return pages.message("Request rejected", "The form was invalid. Start again.", error=True, status=400)
        store.delete_pending(pending["id"])
        if data.get("decision") != "approve":
            store.delete_deletion_request(req["id"])
            return pages.message("Nothing was deleted", "Your account and your data are untouched.")
        keycloak_id = str(user.get("identity", "")).rpartition("#")[2]
        cookie = store.arm_deletion_return(req["id"], keycloak_id, RETURN_TTL)
        action_id = store.create_pending(DELETE_ACTION, req["id"])
        try:
            response = await identity.start_action(request, action_id, "delete_account")
        except LoginError as exc:
            return pages.message("Can't continue", str(exc), error=True, status=502)
        response.set_cookie(COOKIE, cookie, max_age=RETURN_TTL, path="/account", httponly=True, samesite="lax",
                            secure=config.public_url.startswith("https://"))
        return response

    def deletion_return(request: Request, action: dict) -> Response:
        """Keycloak came back through /auth/callback: only cancel and errors do (Keycloak 26 never redirects after
        a successful delete_account; it shows its own page)."""
        store.delete_pending(action["id"])
        store.delete_deletion_request(action["params"])      # nothing more can come of this request
        q = request.query_params
        status = q.get("kc_action_status", "")
        if status == "cancelled":
            response = pages.message("Nothing was deleted", "You cancelled at the sign-in provider. Your account and your "
                                     "data are untouched.")
        elif q.get("error") or status == "error":
            response = pages.message("Nothing was deleted", "The sign-in provider could not delete your account, so Inkcast "
                                     "has not erased anything. Try again later, or ask the operator.", error=True)
        else:
            response = pages.message("Nothing was deleted", "The sign-in provider came back without deleting your account. "
                                     "Your data is untouched; start again if you still want to delete it.")
        response.delete_cookie(COOKIE, path="/account")      # so a later "Back to Application" is the neutral page
        return response

    async def account_return(request: Request) -> Response:
        """Where Keycloak's info pages send the browser "Back to Application" (the client's baseUrl): after an
        email verification, a password reset, a deletion... Without a cookie that matches a pending deletion it is a
        neutral page that says nothing about deletion. With one, the pending record outlives the erasure (the sync's
        DELETE_ACCOUNT pass may get there first), so the person always gets the right answer: gone at Keycloak →
        erased (idempotent; counted as via=return only when this visit erased rows) and "deleted"; Keycloak still
        has them → nothing erased, "nothing was deleted" (the record stays for a later confirmation); Keycloak not
        answering → nothing erased, "being finished". The deleted and finishing pages say what stays for a while:
        copies in backups, until they age out (issue #94)."""
        cookie = request.cookies.get(COOKIE, "")
        req = store.deletion_request_by_cookie(cookie) if cookie else None
        if not req:                              # no cookie, or one no pending deletion matches: nothing to say
            return pages.message("Back at Inkcast", "You're back at Inkcast. You can close this tab and return to your "
                                 "assistant or your MCP client.")
        finishing = pages.deletion_finishing(config.backup_keep)
        if not req.get("keycloak_id") or not config.keycloak_sync_client_id:
            return finishing
        from .keycloak_sync import KeycloakSync, SyncError, erase_person
        sync = ctx.extras.get("keycloak_sync") or ctx.extras.setdefault(
            "keycloak_sync", KeycloakSync(config, store, ctx.http, ctx.metrics))
        try:
            state = await sync.person(req["keycloak_id"])
        except (SyncError, httpx2.HTTPError, ValueError):
            state = "unknown"
        if state == "unknown":                                   # Keycloak did not answer: nothing is erased
            return finishing
        if state != "deleted":                                   # Keycloak still has them: nothing is erased
            return pages.message("Nothing was deleted", "The sign-in provider still has your account, so Inkcast has "
                                 "erased nothing. If you still want it gone, confirm the deletion there, or start again "
                                 "from your MCP client or the API.")
        erase_person(store, ctx.metrics, req["user_id"], "return")
        store.delete_deletion_request(req["id"])
        response = pages.account_deleted(config.backup_keep)
        response.delete_cookie(COOKIE, path="/account")
        return response

    async def account_deletion_link(request: Request, who: Principal):
        return deletion_link(config, store, who.user_id)

    # ---- personal access tokens: REST (OAuth principals only; a PAT can't mint PATs) ----

    async def list_tokens(request: Request, who: Principal):
        return [_info(r) for r in store.list_api_tokens(who.user_id)]

    async def create_token(request: Request, who: Principal):
        req = TokenCreate.model_validate(await json_body(request))
        days = _expiry(req)
        expires = time.time() + days * 86400 if days else None
        token, row = store.create_api_token(who.user_id, req.name, _check_scopes(req.scopes), expires)
        return TokenCreated(**_info(row).model_dump(), token=token)

    async def revoke(request: Request, who: Principal):
        if not store.revoke_api_token(who.user_id, request.path_params["token_id"]):
            raise ApiError(404, "not_found", "No such active token.")
        return {"revoked": True}

    return [
        *routes,
        Route("/auth/login", auth_login, methods=["GET"]),
        # POST is the password form: a guess per request, so it is rate limited per client address. GET too: a GET
        # can carry the same form, and for an IdP each callback is a code exchange with it.
        Route("/auth/callback", limited(auth_callback, ctx.limiter, "login", {"GET", "POST"}), methods=["GET", "POST"]),
        Route("/auth/consent", auth_consent, methods=["GET", "POST"]),
        Route("/tokens/new", limited(tokens_new, ctx.limiter, "tokens-new", {"GET"}), methods=["GET"]),  # a GET that writes
        Route("/account/delete/{ticket}", limited(account_delete, ctx.limiter, "account-delete", {"GET"}), methods=["GET"]),
        Route("/account/return", account_return, methods=["GET"]),
        Route("/api/v1/account/deletion-link", authed(None, account_deletion_link, oauth_only=True), methods=["POST"]),
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

__all__ = ["DELETE_ACTION", "DELETE_REQUEST", "SERVICE", "TOKEN_REQUEST"]
