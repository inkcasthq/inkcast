# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""REST API v1, its OpenAPI document, and the Swagger UI / ReDoc pages.

The docs pages render from bundles vendored in the wheel (static/vendor, pinned and
hash-locked in ASSETS.lock), never from a CDN: they work on air-gapped clusters and
under a strict Content-Security-Policy (no inline scripts).

Routes
  /api                Swagger UI
  /api/docs           Swagger UI (conventional path)
  /api/redoc          ReDoc reference
  /api/openapi.json   OpenAPI 3.1 document
  /api/static/<file>  vendored bundles + init scripts (allow-listed, long-cached)
  /api/v1/...         the REST API (Bearer token, scope `inkcast`)
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError
from pydantic.json_schema import models_json_schema
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .models import (
    ConnectLink, DestinationInfo, ErrorBody, Me, PlatformInfo, PostInfo, PublishRequest, PublishResponse,
)
from .service import InkcastService, ServiceError

SCOPE = "inkcast"
STATIC = files("inkcast") / "static"

# ---------------------------------------------------------------------------------------------
# Static assets: vendored bundles (hash-locked) + our own small init scripts
# ---------------------------------------------------------------------------------------------


@lru_cache(maxsize=None)
def vendor_lock() -> dict[str, str]:
    entries = {}
    for line in (STATIC / "vendor" / "ASSETS.lock").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            digest, name = line.split(None, 1)
            entries[name.strip()] = digest
    return entries


@lru_cache(maxsize=None)
def _asset(name: str) -> tuple[bytes, str, str] | None:
    """(bytes, content type, etag) for an allow-listed asset, verified against the lock."""
    types = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
    if name in vendor_lock():
        data = (STATIC / "vendor" / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != vendor_lock()[name]:
            raise RuntimeError(f"Vendored asset {name} does not match ASSETS.lock; refusing to serve it.")
    elif name in ("swagger-init.js",):
        data = (STATIC / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
    else:
        return None
    return data, types[name[name.rfind("."):]], digest


def asset_url(name: str) -> str:
    asset = _asset(name)
    return f"/api/static/{name}?v={asset[2][:12]}" if asset else f"/api/static/{name}"


async def static_asset(request: Request) -> Response:
    asset = _asset(request.path_params["name"])
    if not asset:
        return Response(status_code=404)
    data, ctype, digest = asset
    etag = f'"{digest[:32]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    # URLs carry ?v=<hash>, so the content behind a URL never changes: cache for a year.
    return Response(data, media_type=ctype, headers={
        "ETag": etag, "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


_DOCS_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
             "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; "
             "base-uri 'none'; form-action 'self'")


def _docs_page(title: str, body: str) -> HTMLResponse:
    html = (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{title}</title>{body}")
    return HTMLResponse(html, headers={"Content-Security-Policy": _DOCS_CSP, "X-Frame-Options": "DENY",
                                       "Referrer-Policy": "no-referrer", "Cache-Control": "no-cache"})


async def swagger_ui(_: Request) -> Response:
    return _docs_page("Inkcast API — Swagger UI", (
        f"<link rel=stylesheet href='{asset_url('swagger-ui.css')}'></head><body>"
        f"<div id=swagger-ui data-spec-url='/api/openapi.json'></div>"
        f"<script src='{asset_url('swagger-ui-bundle.js')}'></script>"
        f"<script src='{asset_url('swagger-init.js')}'></script></body></html>"))


async def redoc(_: Request) -> Response:
    return _docs_page("Inkcast API — ReDoc", (
        "<style>body{margin:0}</style></head><body>"
        "<redoc spec-url='/api/openapi.json' hide-download-button='false'></redoc>"
        f"<script src='{asset_url('redoc.standalone.js')}'></script></body></html>"))


# ---------------------------------------------------------------------------------------------
# OpenAPI document
# ---------------------------------------------------------------------------------------------

_ERR = {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}}


def _resp(model: str | None, description: str, array: bool = False) -> dict[str, Any]:
    if not model:
        return {"description": description}
    schema: dict[str, Any] = {"$ref": f"#/components/schemas/{model}"}
    if array:
        schema = {"type": "array", "items": schema}
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _op(op_id: str, summary: str, tag: str, ok: dict, *, body: str | None = None, params: list | None = None,
        errors: tuple[int, ...] = ()) -> dict[str, Any]:
    op: dict[str, Any] = {"operationId": op_id, "summary": summary, "tags": [tag], "responses": {"200": ok}}
    if body:
        op["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{body}"}}}}
    if params:
        op["parameters"] = params
    for code in (401, *errors):
        op["responses"][str(code)] = {"description": {401: "Missing, invalid or expired token", 404: "Not found",
                                                      409: "Conflict", 422: "Invalid input",
                                                      502: "The platform returned an error"}[code], "content": _ERR}
    return op


def _path_param(name: str, description: str) -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "description": description, "schema": {"type": "string"}}


def _query_int(name: str, default: int, maximum: int, description: str) -> dict[str, Any]:
    return {"name": name, "in": "query", "required": False, "description": description,
            "schema": {"type": "integer", "minimum": 0, "maximum": maximum, "default": default}}


@lru_cache(maxsize=None)
def openapi_document(public_url: str) -> dict[str, Any]:
    _, defs = models_json_schema(
        [(m, "validation" if m is PublishRequest else "serialization")
         for m in (ErrorBody, Me, PlatformInfo, ConnectLink, DestinationInfo, PublishRequest, PublishResponse, PostInfo)],
        ref_template="#/components/schemas/{model}",
    )
    pid = _path_param("platform_id", "Platform id, e.g. `devto`.")
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Inkcast API",
            "version": __version__,
            "description": (
                "Publish and schedule markdown stories across writing platforms.\n\n"
                "The same operations are available to AI assistants as MCP tools at `/mcp`.\n\n"
                "**Authentication:** a Bearer token with the `inkcast` scope — an OAuth access token from this "
                "server's authorization endpoints (discovery at `/.well-known/oauth-authorization-server`).\n\n"
                "**Platform credentials are never accepted by this API.** Use `connect-link` and have the person "
                "enter them on the returned one-time page."),
        },
        "servers": [{"url": public_url}],
        "security": [{"bearerAuth": [SCOPE]}],
        "tags": [{"name": "Account"}, {"name": "Platforms"}, {"name": "Posts"}],
        "paths": {
            "/api/v1/me": {"get": _op("getMe", "The signed-in user", "Account", _resp("Me", "The user"))},
            "/api/v1/platforms": {"get": _op("listPlatforms", "Platforms and connection status", "Platforms",
                                             _resp("PlatformInfo", "Platforms", array=True))},
            "/api/v1/platforms/{platform_id}/connect-link": {"post": _op(
                "createConnectLink", "One-time link to connect a platform", "Platforms",
                _resp("ConnectLink", "The link"), params=[pid], errors=(404,))},
            "/api/v1/platforms/{platform_id}/connection": {"delete": _op(
                "disconnectPlatform", "Delete stored credentials for a platform", "Platforms",
                _resp(None, "Disconnected"), params=[pid], errors=(404,))},
            "/api/v1/platforms/{platform_id}/destinations": {"get": _op(
                "listDestinations", "Publications / organizations you can post to", "Platforms",
                _resp("DestinationInfo", "Destinations", array=True), params=[pid], errors=(404, 409, 502))},
            "/api/v1/posts": {
                "post": _op("publish", "Publish, schedule, draft or preview a story", "Posts",
                            _resp("PublishResponse", "Per-platform results"), body="PublishRequest", errors=(422,)),
                "get": _op("listPosts", "Your posts in a time window", "Posts",
                           _resp("PostInfo", "Posts", array=True),
                           params=[_query_int("days_back", 7, 366, "Include posts from this many days ago."),
                                   _query_int("days_ahead", 90, 730, "Include posts up to this many days ahead.")])},
            "/api/v1/posts/{post_id}/cancel": {"post": _op(
                "cancelPost", "Cancel a scheduled post", "Posts", _resp("PostInfo", "The cancelled post"),
                params=[_path_param("post_id", "Post id.")], errors=(404, 409))},
        },
        "components": {
            "schemas": defs["$defs"],
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer",
                                               "description": "OAuth access token (scope `inkcast`)."}},
        },
    }


# ---------------------------------------------------------------------------------------------
# REST handlers
# ---------------------------------------------------------------------------------------------

Handler = Callable[[Request, str], Awaitable[Any]]


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(ErrorBody(error=code, message=message).model_dump(), status_code=status, headers=headers)


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value


def build_routes(service: InkcastService, load_access_token: Callable[[str], Awaitable[Any]], public_url: str) -> list[Route]:
    resource_metadata = f"{public_url}/.well-known/oauth-protected-resource/mcp"

    def authed(handler: Handler):
        async def endpoint(request: Request) -> Response:
            auth = request.headers.get("authorization", "")
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            access = await load_access_token(token) if token else None
            if not access or SCOPE not in access.scopes or not access.subject:
                return _error(401, "invalid_token", "Send a valid Bearer token with the `inkcast` scope.",
                              {"WWW-Authenticate": f'Bearer resource_metadata="{resource_metadata}", scope="{SCOPE}"'})
            try:
                result = await handler(request, access.subject)
            except ServiceError as exc:
                return _error(exc.status, exc.code, exc.message)
            except ValidationError as exc:
                return _error(422, "invalid_request", "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
            if isinstance(result, Response):
                return result
            return JSONResponse(_dump(result), headers={"Cache-Control": "no-store"})
        return endpoint

    def _int(request: Request, name: str, default: int, maximum: int) -> int:
        raw = request.query_params.get(name)
        try:
            value = int(raw) if raw is not None else default
        except ValueError:
            raise ServiceError(422, "invalid_request", f"{name} must be an integer.") from None
        if not 0 <= value <= maximum:
            raise ServiceError(422, "invalid_request", f"{name} must be between 0 and {maximum}.")
        return value

    async def me(request, user):
        return service.me(user)

    async def platforms(request, user):
        return service.list_platforms(user)

    async def connect_link(request, user):
        return service.connect_link(user, request.path_params["platform_id"])

    async def disconnect(request, user):
        removed = service.disconnect(user, request.path_params["platform_id"])
        return {"disconnected": removed}

    async def destinations(request, user):
        return await service.destinations(user, request.path_params["platform_id"])

    async def posts(request, user):
        if request.method == "POST":
            try:
                body = await request.json()
            except (ValueError, json.JSONDecodeError):
                raise ServiceError(422, "invalid_request", "The body must be JSON.") from None
            return await service.publish(user, PublishRequest.model_validate(body))
        return service.list_posts(user, _int(request, "days_back", 7, 366), _int(request, "days_ahead", 90, 730))

    async def cancel(request, user):
        return service.cancel(user, request.path_params["post_id"])

    async def openapi_json(_: Request) -> Response:
        return JSONResponse(openapi_document(public_url), headers={"Cache-Control": "no-cache"})

    return [
        Route("/api", swagger_ui, methods=["GET"]),
        Route("/api/docs", swagger_ui, methods=["GET"]),
        Route("/api/redoc", redoc, methods=["GET"]),
        Route("/api/openapi.json", openapi_json, methods=["GET"]),
        Route("/api/static/{name}", static_asset, methods=["GET"]),
        Route("/api/v1/me", authed(me), methods=["GET"]),
        Route("/api/v1/platforms", authed(platforms), methods=["GET"]),
        Route("/api/v1/platforms/{platform_id}/connect-link", authed(connect_link), methods=["POST"]),
        Route("/api/v1/platforms/{platform_id}/connection", authed(disconnect), methods=["DELETE"]),
        Route("/api/v1/platforms/{platform_id}/destinations", authed(destinations), methods=["GET"]),
        Route("/api/v1/posts", authed(posts), methods=["GET", "POST"]),
        Route("/api/v1/posts/{post_id}/cancel", authed(cancel), methods=["POST"]),
    ]
