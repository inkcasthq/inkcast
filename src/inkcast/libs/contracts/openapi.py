# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The public OpenAPI 3.1 document for every service behind the gateway (contract-first).

One document describes the whole public API whatever the service split; each operation is
tagged with the service that owns it (``x-inkcast-service``).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic.json_schema import models_json_schema

from ... import __version__
from ..auth import SCOPE, SCOPES
from ..content.story import SCHEDULE_HORIZON_DAYS
from .models import (
    ConnectLink, DeletionLink, DestinationInfo, ErrorBody, InsightsSummary, KpiPoint, KpiSeries, LedgerEntry, Me, PlatformInfo,
    PostInfo, PublishRequest, PublishResponse, RateSummary, SyncRequest, SyncResponse, TokenCreate, TokenCreated, TokenInfo,
)

REQUEST_MODELS = (PublishRequest, SyncRequest, TokenCreate)
MODELS = (ErrorBody, Me, PlatformInfo, ConnectLink, DeletionLink, DestinationInfo, PublishRequest, PublishResponse, PostInfo,
          TokenCreate, TokenInfo, TokenCreated, SyncRequest, SyncResponse, LedgerEntry, RateSummary, InsightsSummary,
          KpiPoint, KpiSeries)

_ERR = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}}
_ERRORS = {401: "Missing, invalid or expired token; or a personal access token on hold while an operator reviews a "
                "sign-in provider change (error `token_held`)",
           403: "The token lacks the scope this needs", 404: "Not found",
           409: "Conflict", 422: "Invalid input", 502: "The platform returned an error"}


def _resp(model: str | None, description: str, array: bool = False) -> dict[str, Any]:
    if not model:
        return {"description": description}
    schema: dict[str, Any] = {"$ref": f"#/components/schemas/{model}"}
    if array:
        schema = {"type": "array", "items": schema}
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _op(op_id: str, summary: str, tag: str, ok: dict, *, scope: str | None, service: str, body: str | None = None,
        params: list | None = None, errors: tuple[int, ...] = (), description: str | None = None) -> dict[str, Any]:
    op: dict[str, Any] = {"operationId": op_id, "summary": summary, "tags": [tag], "responses": {"200": ok},
                          "x-inkcast-service": service}
    if description:
        op["description"] = description
    if scope:
        op["security"] = [{"bearerAuth": [scope]}]
        op["x-inkcast-scope"] = scope
    if body:
        op["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{body}"}}}}
    if params:
        op["parameters"] = params
    for code in (401, 403, *errors):
        op["responses"][str(code)] = {"description": _ERRORS[code], "content": _ERR}
    return op


def _path_param(name: str, description: str) -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "description": description, "schema": {"type": "string"}}


def _query_int(name: str, default: int, maximum: int, description: str) -> dict[str, Any]:
    return {"name": name, "in": "query", "required": False, "description": description,
            "schema": {"type": "integer", "minimum": 0, "maximum": maximum, "default": default}}


_SCOPE_TABLE = "\n".join(f"| `{k}` | {v} |" for k, v in SCOPES.items())


@lru_cache(maxsize=None)
def openapi_document(public_url: str) -> dict[str, Any]:
    _, defs = models_json_schema(
        [(m, "validation" if m in REQUEST_MODELS else "serialization") for m in MODELS],
        ref_template="#/components/schemas/{model}",
    )
    pid = _path_param("platform_id", "Platform id, e.g. `devto`.")
    pub, idn, ins = "PublishingApi", "IdentityService", "InsightsService"
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Inkcast API",
            "version": __version__,
            "description": (
                "Publish and schedule markdown stories across writing platforms.\n\n"
                "The same operations are available to AI assistants as MCP tools at `/mcp`.\n\n"
                "**Authentication:** a Bearer token, either\n"
                "- an OAuth access token from this server's authorization endpoints (discovery at "
                "`/.well-known/oauth-authorization-server`), scope `inkcast`, which allows everything; or\n"
                "- a **personal access token** (`ink_pat_…`, create one with `POST /api/v1/tokens`), which allows "
                "only its scopes:\n\n| Scope | Allows |\n|---|---|\n" + _SCOPE_TABLE + "\n\n"
                "Errors are RFC 9457 `application/problem+json`, with Inkcast's `error` code and `message`.\n\n"
                "**Platform credentials are never accepted by this API.** Use `connect-link` and have the person "
                "enter them on the returned one-time page."),
        },
        "servers": [{"url": public_url}],
        "security": [{"bearerAuth": [SCOPE]}],
        "tags": [{"name": "Account"}, {"name": "Tokens"}, {"name": "Platforms"}, {"name": "Posts"},
                 {"name": "Git publishing"}, {"name": "Insights"}],
        "paths": {
            "/api/v1/me": {"get": _op("getMe", "The signed-in user", "Account", _resp("Me", "The user"),
                                      scope=None, service=pub)},
            "/api/v1/account/deletion-link": {"post": _op(
                "createDeletionLink", "One-time link to delete your account", "Account", _resp("DeletionLink", "The link"),
                scope=None, service=idn, errors=(409,), description="Needs an OAuth token. The person opens the link, "
                "signs in again, reads what goes and what stays, and continues to the sign-in provider (Keycloak), which "
                "asks them to confirm and deletes the account; Inkcast then erases their data. Nothing is deleted by this "
                "call. 409 when the sign-in provider or its user sync is not configured for it.")},
            "/api/v1/tokens": {
                "get": _op("listTokens", "Your personal access tokens", "Tokens", _resp("TokenInfo", "Tokens", array=True),
                           scope=None, service=idn, description="Needs an OAuth token; personal access tokens can't "
                           "manage tokens."),
                "post": _op("createToken", "Create a personal access token (shown once)", "Tokens",
                            _resp("TokenCreated", "The new token"), scope=None, service=idn, body="TokenCreate",
                            errors=(422,), description="Needs an OAuth token. Store the returned `token` as a CI "
                            "secret; Inkcast only keeps its hash.")},
            "/api/v1/tokens/{token_id}": {"delete": _op(
                "revokeToken", "Revoke a personal access token", "Tokens", _resp(None, "Revoked"),
                scope=None, service=idn, params=[_path_param("token_id", "Token id, e.g. `tok_1a2b3c4d5e6f`.")],
                errors=(404,))},
            "/api/v1/platforms": {"get": _op("listPlatforms", "Platforms and connection status", "Platforms",
                                             _resp("PlatformInfo", "Platforms", array=True),
                                             scope="connections:read", service=pub)},
            "/api/v1/platforms/{platform_id}/connect-link": {"post": _op(
                "createConnectLink", "One-time link to connect a platform", "Platforms",
                _resp("ConnectLink", "The link"), scope="connections:write", service=pub, params=[pid], errors=(404,))},
            "/api/v1/platforms/{platform_id}/connection": {"delete": _op(
                "disconnectPlatform", "Delete stored credentials for a platform", "Platforms",
                _resp(None, "Disconnected"), scope="connections:write", service=pub, params=[pid], errors=(404,))},
            "/api/v1/platforms/{platform_id}/destinations": {"get": _op(
                "listDestinations", "Publications / organizations you can post to", "Platforms",
                _resp("DestinationInfo", "Destinations", array=True), scope="connections:read", service=pub,
                params=[pid], errors=(404, 409, 502))},
            "/api/v1/posts": {
                "post": _op("publish", "Publish, schedule, draft or preview a story", "Posts",
                            _resp("PublishResponse", "Per-platform results"), scope="posts:write", service=pub,
                            body="PublishRequest", errors=(422,),
                            description="A dry run (`dry_run: true`) needs only `posts:read`."),
                "get": _op("listPosts", "Your posts in a time window", "Posts",
                           _resp("PostInfo", "Posts", array=True), scope="posts:read", service=pub,
                           params=[_query_int("days_back", 7, 366, "Include posts from this many days ago."),
                                   _query_int("days_ahead", 90, SCHEDULE_HORIZON_DAYS,
                                              "Include posts up to this many days ahead; the maximum is the "
                                              "scheduling horizon, so it covers every scheduled post.")])},
            "/api/v1/posts/{post_id}/cancel": {"post": _op(
                "cancelPost", "Cancel a scheduled or recovering post", "Posts", _resp("PostInfo", "The cancelled post"),
                scope="posts:write", service=pub, params=[_path_param("post_id", "Post id.")], errors=(404, 409),
                description="A `recovering` post (its send was interrupted; the platform has not said whether it arrived) "
                            "is the person's to cancel: check the platform first. A cancel wins over a later answer.")},
            "/api/v1/preview": {"post": _op(
                "preview", "Preview exactly what each platform would receive", "Posts",
                _resp("PublishResponse", "Per-platform previews"), scope="posts:read", service=pub,
                body="PublishRequest", errors=(422,),
                description="Same rendering as publishing (`prepare()`); nothing is sent or stored.")},
            "/api/v1/stories:sync": {"post": _op(
                "syncStories", "Publish or update markdown files from a repository", "Git publishing",
                _resp("SyncResponse", "What happened to each file on each platform"), scope="stories:sync",
                service=pub, body="SyncRequest", errors=(409, 422),
                description=("Idempotent: files are matched to earlier posts through the publish ledger by "
                             "(source, path, platform). Unchanged content does nothing; changed content updates "
                             "the post in place where the platform allows it (DEV) and is reported `stale` where "
                             "it doesn't (Medium), never duplicated. Use the `Idempotency-Key` header to make "
                             "client retries safe: a repeat within 24 hours replays the first response "
                             "(`Idempotent-Replayed: true`); a repeat while the first is still running gets 409 "
                             "`idempotency_key_in_use` (retry shortly); the same key with a different body gets "
                             "422 `idempotency_key_reused`."))},
            "/api/v1/ledger": {"get": _op(
                "listLedger", "What each synced file became on each platform", "Git publishing",
                _resp("LedgerEntry", "Ledger entries", array=True), scope="posts:read", service=pub,
                params=[{"name": "source", "in": "query", "required": False, "schema": {"type": "string"},
                         "description": "Only this repository, e.g. `github.com/acme/blog`."}])},
            "/api/v1/insights/summary": {"get": _op(
                "insightsSummary", "Your publishing numbers for a period", "Insights",
                _resp("InsightsSummary", "Posts by status and platform, success and on-time rates, Git sync outcomes"),
                scope="insights:read", service=ins,
                params=[_query_int("days", 30, 366, "The period, ending today (UTC).")],
                description="Live counts come from the posts and ledger tables; rates come from KPI rollups updated "
                            "a few seconds after each event.")},
            "/api/v1/insights/kpis": {"get": _op(
                "insightsKpis", "Time series of your KPIs", "Insights", _resp("KpiSeries", "One series per KPI", array=True),
                scope="insights:read", service=ins, errors=(422,),
                params=[{"name": "names", "in": "query", "required": True, "schema": {"type": "string"},
                         "description": "Comma-separated KPI names, e.g. `posts_published,posts_failed,sync_results`."},
                        {"name": "grain", "in": "query", "required": False, "schema": {"type": "string", "enum": ["hour", "day"],
                                                                                        "default": "day"},
                         "description": "`hour` covers at most 31 days."},
                        _query_int("days", 30, 366, "The period, ending today (UTC).")])},
        },
        "components": {
            "schemas": defs["$defs"],
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer",
                                               "description": "OAuth access token (scope `inkcast`) or personal access token."}},
        },
    }
