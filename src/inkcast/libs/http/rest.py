# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""REST plumbing shared by every service: bearer auth with scopes, errors as RFC 9457 problem+json.

Error bodies keep Inkcast's ``error`` (code) and ``message`` fields alongside the RFC 9457
``type``/``title``/``status``/``detail`` members, so existing clients keep working.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..auth import SCOPE, Principal, TokenVerifier


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def problem(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "Error"
    body = {"type": "about:blank", "title": title, "status": status, "detail": message, "error": code, "message": message}
    return JSONResponse(body, status_code=status, headers=headers, media_type="application/problem+json")


def dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [dump(v) for v in value]
    return value


async def json_body(request: Request) -> Any:
    try:
        return await request.json()
    except (ValueError, json.JSONDecodeError):
        raise ApiError(422, "invalid_request", "The body must be JSON.") from None


def query_int(request: Request, name: str, default: int, maximum: int) -> int:
    raw = request.query_params.get(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        raise ApiError(422, "invalid_request", f"{name} must be an integer.") from None
    if not 0 <= value <= maximum:
        raise ApiError(422, "invalid_request", f"{name} must be between 0 and {maximum}.")
    return value


Handler = Callable[[Request, Principal], Awaitable[Any]]


class Authed:
    """Wrap handlers so they run only for a valid bearer token that holds the route's scope."""

    def __init__(self, verifier: TokenVerifier, public_url: str):
        self.verifier = verifier
        self.resource_metadata = f"{public_url}/.well-known/oauth-protected-resource/mcp"

    def __call__(self, scope: str | None, handler: Handler, *, oauth_only: bool = False):
        async def endpoint(request: Request) -> Response:
            auth = request.headers.get("authorization", "")
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            principal = self.verifier.principal(token)
            if principal is None:
                return problem(401, "invalid_token", "Send a valid Bearer token (an OAuth access token with the "
                               "`inkcast` scope, or a personal access token).",
                               {"WWW-Authenticate": f'Bearer resource_metadata="{self.resource_metadata}", scope="{SCOPE}"'})
            if oauth_only and principal.kind != "oauth":
                return problem(403, "forbidden", "Personal access tokens can't manage tokens; sign in with OAuth "
                               "(e.g. from Claude Code) to do this.")
            if scope and not principal.can(scope):
                return problem(403, "insufficient_scope", f"This token lacks the `{scope}` scope.",
                               {"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'})
            try:
                result = await handler(request, principal)
            except ApiError as exc:
                return problem(exc.status, exc.code, exc.message)
            except ValidationError as exc:
                return problem(422, "invalid_request", "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
            if isinstance(result, Response):
                return result
            return JSONResponse(dump(result), headers={"Cache-Control": "no-store"})
        return endpoint
