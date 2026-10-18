# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Gateway: the built-in edge for local runs and single-node installs.

It routes each request by the public route table (libs/contracts/routes.py) to the upstream
service's base URL (INKCAST_UPSTREAM_<NAME>, which may include a path prefix such as
``http://localhost:8001/publishing-api``). In clusters, Kubernetes Gateway API HTTPRoutes built
from the same table do this job instead; see deploy/kustomize/components/gateway-api.

The Host header is passed through unchanged (services check it against INKCAST_PUBLIC_URL to
block DNS rebinding). X-Forwarded-For/-Proto/-Host are set from what *this* process knows (the peer
address and scheme after uvicorn applied INKCAST_FORWARDED_ALLOW_IPS), never copied from the client,
so a caller can't spoof its address or scheme to the services. The request id is forwarded, so one
id follows a request through every service's log. Responses stream back as-is, redirects included.
The gateway itself keeps no state and needs no database.
"""

from __future__ import annotations

import logging

import httpx2
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from ...libs.contracts.routes import match
from ...libs.http.rest import problem
from ...libs.observability import current_request_id
from ...libs.runtime import Context
from ...libs.service import ServiceSpec, full_name

log = logging.getLogger(__name__)

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "content-length"}
# Never forwarded from the client: the gateway sets its own, from what it knows.
NOT_FORWARDED = {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port", "forwarded",
                 "x-real-ip", "x-request-id"}
TOO_LARGE = "The request body is too large."


async def read_body(request: Request, limit: int) -> bytes | None:
    """The whole body, or None when it exceeds ``limit`` (checked up front and while streaming)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def _routes(ctx: Context) -> list[Route]:
    upstreams = ctx.config.upstreams
    client = httpx2.AsyncClient(timeout=httpx2.Timeout(60.0, connect=5.0), follow_redirects=False)
    ctx.extras["gateway_client"] = client

    async def proxy(request: Request) -> Response:
        path = request.url.path
        service = match(path)
        if service is None:
            return problem(404, "not_found", f"Nothing is served at {path}.")
        base = upstreams.get(full_name(service))
        if not base:
            return problem(502, "no_upstream", f"The gateway has no upstream for {service} "
                           f"(set INKCAST_UPSTREAM_{full_name(service).upper().replace('-', '_')}).")
        body = await read_body(request, ctx.config.max_body_bytes)
        if body is None:
            return problem(413, "too_large", TOO_LARGE)
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP | NOT_FORWARDED]
        # request.client and request.url.scheme already reflect a trusted upstream proxy (uvicorn's
        # proxy-headers handling with INKCAST_FORWARDED_ALLOW_IPS); anything else the client sent is dropped.
        headers += [("x-forwarded-for", request.client.host if request.client else ""),
                    ("x-forwarded-proto", request.url.scheme),
                    ("x-forwarded-host", request.headers.get("host", "")),
                    ("x-request-id", current_request_id())]
        url = base + path + (f"?{request.url.query}" if request.url.query else "")
        try:
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                         stream=True)
        except httpx2.HTTPError as exc:
            ctx.metrics.inc("inkcast_gateway_requests_total", upstream=full_name(service), code="error")
            log.warning("Gateway: %s unreachable: %s", service, exc,
                        extra={"event": "gateway.upstream.unreachable", "upstream": full_name(service)})
            return problem(502, "upstream_unreachable", f"{service} is unreachable.")
        ctx.metrics.inc("inkcast_gateway_requests_total", upstream=full_name(service), code=f"{upstream.status_code // 100}xx")
        out_headers = [(k, v) for k, v in upstream.headers.multi_items() if k.lower() not in HOP_BY_HOP]
        response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                     background=BackgroundTask(upstream.aclose))
        response.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in out_headers]
        return response

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    return [Route("/{path:path}", proxy, methods=methods)]


SERVICE = ServiceSpec(
    name="Gateway",
    summary="Built-in edge: routes public paths to services (local and single-node; clusters use Gateway API)",
    routes=_routes,
    needs=("upstreams",),
)
