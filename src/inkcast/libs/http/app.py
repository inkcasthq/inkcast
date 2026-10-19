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
from ..secrets import run_checks
from ..service import ServiceSpec
from .middleware import RequestContext


def _service_app(specs: Iterable[ServiceSpec], ctx: Context) -> Starlette:
    """One Starlette app holding these services' routes; the first spec with a ``base`` supplies it."""
    specs = list(specs)
    base = next((s.base for s in specs if s.base), None)
    app = base(ctx) if base else Starlette()
    for spec in specs:
        if spec.routes:
            app.router.routes.extend(spec.routes(ctx))
    return app


async def readiness(specs: Iterable[ServiceSpec], ctx: Context) -> list[str]:
    problems = []
    if ctx.store is not None:
        try:
            ctx.store.ping()
        except Exception as exc:  # pragma: no cover - depends on storage failure
            problems.append(f"storage: {exc}")
    if ctx.authz is not None:  # the engine caches its answer, so this never hammers OpenFGA (E3.F3.6.S4)
        problem = await ctx.authz.ready()
        if problem:
            problems.append(f"authz ({ctx.authz.name}): {problem}")
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
    """Kubernetes-style probes. /livez: the process serves requests. /readyz: storage answers, the policy engine's
    store and model exist, the scheduler loop (if this process runs it) ticked recently, and each service's own
    checks pass."""
    specs = list(specs)

    async def livez(_: Request) -> Response:
        return JSONResponse({"ok": True})

    async def readyz(_: Request) -> Response:
        problems = await readiness(specs, ctx)
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


def wrap(app, ctx: Context) -> RequestContext:
    """The outermost layer: request ids, the access log, body limits and security headers (middleware.py)."""
    return RequestContext(app, max_body=ctx.config.max_body_bytes, hsts=ctx.config.public_url.startswith("https://"),
                          metrics=ctx.metrics)


def compose(specs: Iterable[ServiceSpec], ctx: Context, prefix: str | None = None):
    """The ASGI app for these services at ``prefix``. Every request passes through ``RequestContext`` first."""
    specs = [s for s in specs if s.kind == "http"]
    prefix = ctx.config.path_prefix if prefix is None else prefix
    probes = probe_routes(specs, ctx)

    if prefix != "auto" and resolve_prefix(prefix) == "/":
        app = _service_app(specs, ctx)
        app.router.routes[0:0] = probes  # before any catch-all route (the Gateway has one)
        return wrap(app, ctx)

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

    return wrap(Starlette(routes=[*probes, *(Mount(path, app=sub) for path, sub in inner)], lifespan=lifespan), ctx)


def secrets_status(ctx: Context) -> dict:
    """Metadata about this process's secrets and posture. Contains no secret material: names, sources,
    fingerprints (HMAC under a per-process key), ages, counts and the check table."""
    provider = ctx.config.secrets
    return {
        "mode": provider.mode if provider else None,
        "ttl_seconds": provider.ttl if provider else None,
        "secrets": provider.status() if provider else [],
        "checks": [c.as_dict() for c in run_checks(ctx.config)],
    }


def metrics_app(specs: Iterable[ServiceSpec], ctx: Context) -> Starlette:
    """Prometheus /metrics, the probes and /secrets (status metadata, never values), on a separate port that
    is never exposed publicly (the NetworkPolicy admits only the monitoring namespace)."""
    app = ctx.metrics.asgi()

    async def secrets(_: Request) -> Response:
        return JSONResponse(secrets_status(ctx), headers={"Cache-Control": "no-store"})

    app.router.routes.extend([*probe_routes(specs, ctx), Route("/secrets", secrets)])
    return app
