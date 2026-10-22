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
import json
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


# ReDoc draws its right-hand panel (code samples) dark by default; Inkcast's pages are light only (#60). The `theme`
# attribute is JSON that ReDoc parses itself, so no inline script is needed under the CSP.
REDOC_THEME = json.dumps({
    "rightPanel": {"backgroundColor": "#f3f1ea", "textColor": "#1c1b19", "width": "40%"},
    "codeBlock": {"backgroundColor": "#e9e6dc"},
    "typography": {"code": {"color": "#7a2e0e", "backgroundColor": "#efece4"}},
}, separators=(",", ":"))
# What the theme can't reach: ReDoc's code-sample token colours are fixed for a dark panel (keys are plain white), and
# the selected tab's background is the panel's text colour. Its styled-component selectors outrank plain classes.
REDOC_LIGHT_CSS = (
    ".redoc-json .token.property.string{color:#1d4f73!important}"
    ".token.string{color:#2f6b2f!important}"
    ".token.number,.token.property,.token.constant{color:#1d5f8a!important}"
    ".token.boolean{color:#a8321f!important}"
    ".token.keyword{color:#8a3b52!important}"
    ".token.string+a,.token.string+a:visited{color:#0e6b63!important}"
    ".react-tabs__tab--selected{background:#ffffff!important;color:#1c1b19!important}"
)


async def redoc(_: Request) -> Response:
    return _docs_page("Inkcast API — ReDoc", (
        f"<style>body{{margin:0}}{REDOC_LIGHT_CSS}</style></head><body>"
        f"<redoc spec-url='/api/openapi.json' hide-download-button='false' theme='{REDOC_THEME}'></redoc>"
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
