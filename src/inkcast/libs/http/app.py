# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Turn service specs into one ASGI app, at "/" or under each service's full name.

INKCAST_PATH_PREFIX decides where a process serves:

* ``/`` (default): every service's routes at the root, as one app. With ``All`` this is the
  classic single-node Inkcast.
* ``auto``: each service under ``/<full-name>`` (``/publishing-api/api/v1/posts``,
  ``/identity-service/authorize``...). Lets one host or a path-routing ingress address every
  service directly.
* any other path, e.g. ``/inkcast``: all of this process's services under that one path.

Probes (/livez, /readyz, /healthz) always answer at the root as well, and on the metrics port.
"""

from __future__ import annotations

import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Iterable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from ... import __version__
from ..runtime import Context
from ..service import ServiceSpec


def _service_app(specs: Iterable[ServiceSpec], ctx: Context) -> Starlette:
    """One Starlette app holding these services' routes; the first spec with a ``base`` supplies it."""
    specs = list(specs)
    base = next((s.base for s in specs if s.base), None)
    app = base(ctx) if base else Starlette()
    for spec in specs:
        if spec.routes:
            app.router.routes.extend(spec.routes(ctx))
    return app


def readiness(specs: Iterable[ServiceSpec], ctx: Context) -> list[str]:
    problems = []
    if ctx.store is not None:
        try:
            ctx.store.ping()
        except Exception as exc:  # pragma: no cover - depends on storage failure
            problems.append(f"storage: {exc}")
    if ctx.runs_scheduler and ctx.publisher and ctx.publisher.last_tick and \
            time.time() - ctx.publisher.last_tick > max(120, 6 * ctx.config.scheduler_interval_seconds):
        problems.append("scheduler: no tick recently")
    for spec in specs:
        for check in spec.ready_checks:
            problem = check(ctx)
            if problem:
                problems.append(f"{spec.full_name}: {problem}")
    return problems


def probe_routes(specs: Iterable[ServiceSpec], ctx: Context) -> list[Route]:
    """Kubernetes-style probes. /livez: the process serves requests. /readyz: storage answers, the
    scheduler loop (if this process runs it) ticked recently, and each service's own checks pass."""
    specs = list(specs)

    async def livez(_: Request) -> Response:
        return JSONResponse({"ok": True})

    async def readyz(_: Request) -> Response:
        problems = readiness(specs, ctx)
        body = {"ok": not problems, "version": __version__, "platforms": list(ctx.platforms),
                "services": [s.name for s in specs]}
        if problems:
            body["problems"] = problems
        return JSONResponse(body, status_code=200 if not problems else 503)

    return [Route("/livez", livez), Route("/readyz", readyz), Route("/healthz", readyz)]


def resolve_prefix(prefix: str, spec: ServiceSpec | None = None) -> str:
    if prefix == "auto":
        if spec is None:
            raise ValueError("'auto' needs a service")
        return "/" + spec.full_name
    return "/" + prefix.strip("/") if prefix.strip("/") else "/"


def compose(specs: Iterable[ServiceSpec], ctx: Context, prefix: str | None = None) -> Starlette:
    specs = [s for s in specs if s.kind == "http"]
    prefix = ctx.config.path_prefix if prefix is None else prefix
    probes = probe_routes(specs, ctx)

    if prefix != "auto" and resolve_prefix(prefix) == "/":
        app = _service_app(specs, ctx)
        app.router.routes[0:0] = probes  # before any catch-all route (the Gateway has one)
        return app

    if prefix == "auto":
        inner = [(resolve_prefix("auto", s), _service_app([s], ctx)) for s in specs]
    else:
        inner = [(resolve_prefix(prefix), _service_app(specs, ctx))]

    @asynccontextmanager
    async def lifespan(_app):
        async with AsyncExitStack() as stack:
            for _, sub in inner:
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    return Starlette(routes=[*probes, *(Mount(path, app=sub) for path, sub in inner)], lifespan=lifespan)


def metrics_app(specs: Iterable[ServiceSpec], ctx: Context) -> Starlette:
    """Prometheus /metrics plus probes, on a separate port that is never exposed publicly."""
    app = ctx.metrics.asgi()
    app.router.routes.extend(probe_routes(specs, ctx))
    return app
