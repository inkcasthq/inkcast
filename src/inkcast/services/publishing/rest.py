# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""PublishingApi's REST routes (/api/v1/...), plus the API docs pages.

Every route needs a Bearer token holding the route's scope; see libs/auth and the OpenAPI
document in libs/contracts/openapi.py, which is the contract these handlers implement.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...libs.auth import Principal
from ...libs.content.story import SCHEDULE_HORIZON_DAYS
from ...libs.contracts.models import PublishRequest, SyncRequest
from ...libs.contracts.openapi import openapi_document
from ...libs.http.docs import docs_routes
from ...libs.http.rest import ApiError, Authed, dump, json_body, query_int
from .sync import GitSync
from .usecases import InkcastService

log = logging.getLogger(__name__)


def settle_idempotency_key(user_id: str, what: str, write: Callable[[], bool]) -> None:
    """``write`` releases or completes the request's Idempotency-Key reservation once its sync has run (``what`` says
    which). That it fails (the database busy) must not turn the sync's outcome into a 500: the reservation expires on
    its own. That it matches nothing means the reservation was taken over by a repeat (the sync outlived
    IDEMPOTENCY_PENDING_SECONDS) or the person was erased, and is the repeat's to settle. Both are logged by user id,
    never with the key (#416)."""
    try:
        settled = write()
    except Exception:
        log.exception("Git sync of user %s ended, but its Idempotency-Key reservation could not be %s; it expires on"
                      " its own", user_id, what)
        return
    if not settled:
        log.warning("Git sync of user %s ended after a repeat took its Idempotency-Key reservation over (the sync outlived"
                    " the pending window), or the person was erased: nothing %s", user_id, what)


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
                                  query_int(request, "days_ahead", 90, SCHEDULE_HORIZON_DAYS))

    async def cancel(request: Request, who: Principal):
        return service.cancel(who.user_id, request.path_params["post_id"])

    async def resend(request: Request, who: Principal):
        return await service.resend(who.user_id, request.path_params["post_id"])

    async def preview(request: Request, who: Principal):
        req = PublishRequest.model_validate({**await json_body(request), "dry_run": True})
        return await service.publish(who.user_id, req)

    async def stories_sync(request: Request, who: Principal):
        body = await json_body(request)
        req = SyncRequest.model_validate(body)
        key = request.headers.get("idempotency-key", "").strip()[:200]
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if not key or req.dry_run:
            return await sync.sync(who.user_id, req)
        # Reserve the key before running, so two identical requests can't both run and race to save a response. The
        # reservation is this request's (its owner): one a repeat took over once this sync outlived the pending window
        # is the repeat's to release or complete, not this request's (#416).
        held = sync.store.reserve_idempotency_key(who.user_id, key, fingerprint)
        if isinstance(held, tuple):
            request_hash, response = held
            if request_hash != fingerprint:
                raise ApiError(422, "idempotency_key_reused", "This Idempotency-Key was used for a different request.")
            if not response:
                raise ApiError(409, "idempotency_key_in_use", "A request with this Idempotency-Key is still running. "
                               "Retry in a few seconds to get its result.")
            return JSONResponse(json.loads(response), headers={"Cache-Control": "no-store", "Idempotent-Replayed": "true"})
        owner = held
        release = functools.partial(sync.store.release_idempotency_key, who.user_id, key, owner)
        try:
            result = await sync.sync(who.user_id, req)
        except BaseException:
            settle_idempotency_key(who.user_id, "released", release)
            raise
        if any(r.action in ("skipped", "failed") for r in result.results):
            # A skipped result asks for a re-run: the file was held by another sync, its post was being sent, or its
            # state changed under this one. A failed one is tried again by the next sync (the platform refused it, or
            # its key: reconnect, then re-run). The re-run (the CLI, the action) sends the same body, so the same key: a
            # replay would answer it with this result for 24 hours and sync nothing, and a removal's pending post would
            # go out (#290). So the key is freed and the re-run runs; what this run did holds in the ledger (a post made
            # is 'unchanged', a cancelled post stays cancelled), so a repeat sends nothing twice, replayed or not.
            settle_idempotency_key(who.user_id, "released", release)
        else:
            settle_idempotency_key(who.user_id, "completed", functools.partial(
                sync.store.complete_idempotency_key, who.user_id, key, owner, json.dumps(dump(result))))
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
        Route("/api/v1/posts/{post_id}/resend", authed("posts:write", resend), methods=["POST"]),
        Route("/api/v1/preview", authed("posts:read", preview), methods=["POST"]),
        Route("/api/v1/stories:sync", authed("stories:sync", stories_sync), methods=["POST"]),
        Route("/api/v1/ledger", authed("posts:read", ledger), methods=["GET"]),
    ]
