# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""PublishingApi's REST routes (/api/v1/...), plus the API docs pages.

Every route needs a Bearer token holding the route's scope; see libs/auth and the OpenAPI
document in libs/contracts/openapi.py, which is the contract these handlers implement.
"""

from __future__ import annotations

import hashlib
import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...libs.auth import Principal
from ...libs.contracts.models import PublishRequest, SyncRequest
from ...libs.contracts.openapi import openapi_document
from ...libs.http.docs import docs_routes
from ...libs.http.rest import ApiError, Authed, dump, json_body, query_int
from .sync import GitSync
from .usecases import InkcastService


def build_routes(service: InkcastService, sync: GitSync, authed: Authed, public_url: str) -> list[Route]:
    async def me(request: Request, who: Principal):
        return service.me(who.user_id)

    async def platforms(request: Request, who: Principal):
        return service.list_platforms(who.user_id)

    async def connect_link(request: Request, who: Principal):
        return service.connect_link(who.user_id, request.path_params["platform_id"])

    async def disconnect(request: Request, who: Principal):
        return {"disconnected": service.disconnect(who.user_id, request.path_params["platform_id"])}

    async def destinations(request: Request, who: Principal):
        return await service.destinations(who.user_id, request.path_params["platform_id"])

    async def publish(request: Request, who: Principal):
        req = PublishRequest.model_validate(await json_body(request))
        if not req.dry_run and not who.can("posts:write"):
            raise ApiError(403, "insufficient_scope", "This token lacks the `posts:write` scope (a dry run needs only "
                           "`posts:read`).")
        return await service.publish(who.user_id, req)

    async def list_posts(request: Request, who: Principal):
        return service.list_posts(who.user_id, query_int(request, "days_back", 7, 366),
                                  query_int(request, "days_ahead", 90, 730))

    async def cancel(request: Request, who: Principal):
        return service.cancel(who.user_id, request.path_params["post_id"])

    async def preview(request: Request, who: Principal):
        req = PublishRequest.model_validate({**await json_body(request), "dry_run": True})
        return await service.publish(who.user_id, req)

    async def stories_sync(request: Request, who: Principal):
        body = await json_body(request)
        req = SyncRequest.model_validate(body)
        key = request.headers.get("idempotency-key", "").strip()[:200]
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if key and not req.dry_run:
            seen = sync.store.idempotent_response(who.user_id, key)
            if seen:
                if seen[0] != fingerprint:
                    raise ApiError(422, "idempotency_key_reused", "This Idempotency-Key was used for a different request.")
                return JSONResponse(json.loads(seen[1]), headers={"Cache-Control": "no-store", "Idempotent-Replayed": "true"})
        result = await sync.sync(who.user_id, req)
        if key and not req.dry_run:
            sync.store.save_idempotent_response(who.user_id, key, fingerprint, json.dumps(dump(result)))
        return result

    async def ledger(request: Request, who: Principal):
        return sync.ledger(who.user_id, request.query_params.get("source") or None)

    return [
        *docs_routes(lambda: openapi_document(public_url)),
        Route("/api/v1/me", authed(None, me), methods=["GET"]),
        Route("/api/v1/platforms", authed("connections:read", platforms), methods=["GET"]),
        Route("/api/v1/platforms/{platform_id}/connect-link", authed("connections:write", connect_link), methods=["POST"]),
        Route("/api/v1/platforms/{platform_id}/connection", authed("connections:write", disconnect), methods=["DELETE"]),
        Route("/api/v1/platforms/{platform_id}/destinations", authed("connections:read", destinations), methods=["GET"]),
        Route("/api/v1/posts", authed("posts:read", list_posts), methods=["GET"]),
        Route("/api/v1/posts", authed("posts:read", publish), methods=["POST"]),
        Route("/api/v1/posts/{post_id}/cancel", authed("posts:write", cancel), methods=["POST"]),
        Route("/api/v1/preview", authed("posts:read", preview), methods=["POST"]),
        Route("/api/v1/stories:sync", authed("stories:sync", stories_sync), methods=["POST"]),
        Route("/api/v1/ledger", authed("posts:read", ledger), methods=["GET"]),
    ]
