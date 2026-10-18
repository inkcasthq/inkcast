# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Swagger UI and ReDoc pages served from vendored, hash-locked bundles (never a CDN).

They work on air-gapped clusters and under a strict Content-Security-Policy (no inline scripts).

Routes (see ``docs_routes``)
  /api                Swagger UI
  /api/docs           Swagger UI (conventional path)
  /api/redoc          ReDoc reference
  /api/openapi.json   OpenAPI 3.1 document (from libs.contracts.openapi)
  /api/static/<file>  vendored bundles + init scripts (allow-listed, long-cached)
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib.resources import files
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

STATIC = files("inkcast") / "static"


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


DOCS_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'")


def _docs_page(title: str, body: str) -> HTMLResponse:
    html = (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{title}</title>{body}")
    return HTMLResponse(html, headers={"Content-Security-Policy": DOCS_CSP, "X-Frame-Options": "DENY",
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


def docs_routes(document: Callable[[], dict[str, Any]]) -> list[Route]:
    async def openapi_json(_: Request) -> Response:
        return JSONResponse(document(), headers={"Cache-Control": "no-cache"})

    return [
        Route("/api", swagger_ui, methods=["GET"]),
        Route("/api/docs", swagger_ui, methods=["GET"]),
        Route("/api/redoc", redoc, methods=["GET"]),
        Route("/api/openapi.json", openapi_json, methods=["GET"]),
        Route("/api/static/{name}", static_asset, methods=["GET"]),
    ]
