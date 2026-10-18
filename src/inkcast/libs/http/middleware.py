# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The outermost layer of every HTTP service (``compose`` wraps the app in it):

* **Request id.** ``X-Request-Id`` from the client or the Gateway is kept when it is well-formed
  (letters, digits, ``.-_``, 8–128 chars), otherwise a fresh one is made. It is set in a contextvar so every
  log line of the request carries ``request_id``, and it is echoed in the response.
* **Access log.** One ``http.request`` event per request: method, path (never the query string: OAuth
  codes travel there), status, duration, client. Probes and /metrics are logged at DEBUG only.
* **Body limit.** ``Content-Length`` above ``max_body`` is refused with 413 before a handler runs; a
  chunked body is counted as it streams and cut off at the same limit.
* **Security headers.** ``X-Content-Type-Options: nosniff``, ``Referrer-Policy: no-referrer`` and (behind
  https) ``Strict-Transport-Security`` on every response that does not already set them.

Pure ASGI, no Starlette ``BaseHTTPMiddleware`` (which buffers responses and breaks streaming).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException

from ..observability import request_id_var

log = logging.getLogger("inkcast.http")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
QUIET_PATHS = ("/livez", "/readyz", "/healthz", "/metrics")
DEFAULT_MAX_BODY = 10 * 1024 * 1024
TOO_LARGE = "The request body is too large."


def new_request_id() -> str:
    return uuid.uuid4().hex


def accept_request_id(value: str | None) -> str:
    """The inbound id when it is well-formed (so a Gateway or ingress can set it), else a new one."""
    return value if value and _REQUEST_ID.match(value) else new_request_id()


class RequestContext:
    def __init__(self, app: Callable[..., Awaitable[None]], *, max_body: int = DEFAULT_MAX_BODY, hsts: bool = False):
        self.app = app
        self.max_body = max_body
        self.hsts = hsts

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        request_id = accept_request_id(headers.get("x-request-id"))
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 0
        path = scope.get("path", "")
        method = scope.get("method", "")
        client = (scope.get("client") or ("", 0))[0]

        async def send_wrapped(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                out = MutableHeaders(scope=message)
                out["x-request-id"] = request_id
                out.setdefault("x-content-type-options", "nosniff")
                out.setdefault("referrer-policy", "no-referrer")
                if self.hsts:
                    out.setdefault("strict-transport-security", "max-age=63072000; includeSubDomains")
            await send(message)

        received = 0

        async def receive_capped() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    log.warning("Request body over %d bytes refused", self.max_body,
                                extra={"event": "http.request.rejected", "method": method, "path": path, "reason": "body_too_large"})
                    raise HTTPException(413, TOO_LARGE)
            return message

        try:
            declared = headers.get("content-length", "")
            if declared.isdigit() and int(declared) > self.max_body:
                log.warning("Request body of %s bytes refused (limit %d)", declared, self.max_body,
                            extra={"event": "http.request.rejected", "method": method, "path": path, "reason": "body_too_large"})
                await self._reject(send_wrapped, 413, "too_large", TOO_LARGE)
                return
            await self.app(scope, receive_capped, send_wrapped)
        finally:
            ms = round((time.perf_counter() - started) * 1000, 1)
            level = logging.DEBUG if path in QUIET_PATHS else logging.INFO
            log.log(level, "%s %s -> %s (%sms)", method, path, status or "-", ms,
                    extra={"event": "http.request", "method": method, "path": path, "status": status, "ms": ms,
                           "client": client})
            request_id_var.reset(token)

    @staticmethod
    async def _reject(send: Callable, status: int, code: str, message: str) -> None:
        from .rest import problem  # local import: rest imports auth, which must not depend on this module

        response = problem(status, code, message)
        await send({"type": "http.response.start", "status": response.status_code, "headers": response.raw_headers})
        await send({"type": "http.response.body", "body": response.body})


__all__ = ["DEFAULT_MAX_BODY", "QUIET_PATHS", "RequestContext", "accept_request_id", "new_request_id"]
