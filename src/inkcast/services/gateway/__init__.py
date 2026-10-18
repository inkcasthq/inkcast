# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Gateway: the built-in edge for local runs and single-node installs.

It routes each request by the public route table (libs/contracts/routes.py) to the upstream
service's base URL (INKCAST_UPSTREAM_<NAME>, which may include a path prefix such as
``http://localhost:8001/publishing-api``). In clusters, Kubernetes Gateway API HTTPRoutes built
from the same table do this job instead; see deploy/kustomize/components/gateway-api.

The Host header is passed through unchanged (services check it against INKCAST_PUBLIC_URL to
block DNS rebinding) and X-Forwarded-For/-Proto/-Host are set. Responses stream back as-is,
redirects included. The gateway itself keeps no state and needs no database.
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
from ...libs.runtime import Context
from ...libs.service import ServiceSpec, full_name

log = logging.getLogger(__name__)

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "content-length"}
MAX_BODY = 10 * 1024 * 1024


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
        body = await request.body()
        if len(body) > MAX_BODY:
            return problem(413, "too_large", "The request body is too large.")
        headers = [(k, v) for k, v in request.headers.items()
                   if k.lower() not in HOP_BY_HOP and not k.lower().startswith("x-forwarded-")]
        client_ip = request.client.host if request.client else ""
        prior = request.headers.get("x-forwarded-for")
        headers += [("x-forwarded-for", f"{prior}, {client_ip}" if prior else client_ip),
                    ("x-forwarded-proto", request.headers.get("x-forwarded-proto", request.url.scheme)),
                    ("x-forwarded-host", request.headers.get("host", ""))]
        url = base + path + (f"?{request.url.query}" if request.url.query else "")
        try:
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                         stream=True)
        except httpx2.HTTPError as exc:
            ctx.metrics.inc("inkcast_gateway_requests_total", upstream=full_name(service), code="error")
            log.warning("Gateway: %s unreachable: %s", service, exc)
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
