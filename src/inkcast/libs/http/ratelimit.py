# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A small per-client rate limit for the endpoints anyone can call without a token: dynamic client
registration, the token-request page, the password form and the connect page. They write rows or
compare passwords, so an unbounded caller could fill the database or guess.

Token bucket per (limit name, client address), in process memory. That is the right size for a
single node and a first line of defence behind an ingress; with several replicas each one enforces
its own share, so for real abuse pressure add the limit at the edge (Gateway API / the ingress) too.

    Route("/register", limited(endpoint, limiter, "register"), methods=["POST"])

``limited`` accepts a Starlette endpoint function or an ASGI app (the MCP SDK's routes are the latter)
and answers ``429 application/problem+json`` with ``Retry-After`` when the bucket is empty. Reads
(GET/HEAD/OPTIONS) are never limited.
"""

from __future__ import annotations

import inspect
import threading
import time
from typing import Any, Callable

from starlette.routing import request_response

from .rest import problem

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class RateLimiter:
    """``per_minute`` requests per client, with a burst of the same size. ``per_minute=0`` disables it."""

    def __init__(self, per_minute: int = 60, clock: Callable[[], float] = time.monotonic, max_clients: int = 10_000):
        self.per_minute = per_minute
        self._clock = clock
        self._max_clients = max_clients
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}  # (name, client) -> (tokens, updated_at)
        self._lock = threading.Lock()

    def allow(self, name: str, client: str) -> float:
        """0.0 when the request may proceed, else the seconds to wait."""
        if self.per_minute <= 0:
            return 0.0
        rate = self.per_minute / 60.0
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.get((name, client), (float(self.per_minute), now))
            tokens = min(float(self.per_minute), tokens + (now - updated) * rate)
            if tokens >= 1.0:
                self._buckets[(name, client)] = (tokens - 1.0, now)
                self._trim(now)
                return 0.0
            self._buckets[(name, client)] = (tokens, now)
            return (1.0 - tokens) / rate

    def _trim(self, now: float) -> None:
        if len(self._buckets) <= self._max_clients:
            return
        stale = [k for k, (_, updated) in self._buckets.items() if now - updated > 60.0]
        for k in stale:
            del self._buckets[k]
        while len(self._buckets) > self._max_clients:  # still full: drop the oldest
            del self._buckets[next(iter(self._buckets))]


def limited(endpoint: Any, limiter: RateLimiter, name: str, methods: set[str] = WRITE_METHODS) -> Callable:
    """Wrap a Starlette endpoint function or an ASGI app so requests with these ``methods`` obey ``limiter``
    (writes by default; pass ``{"GET"}`` for a GET that creates state, like /tokens/new)."""
    app = request_response(endpoint) if inspect.isfunction(endpoint) or inspect.ismethod(endpoint) else endpoint
    return _Guarded(app, limiter, name, methods)


class _Guarded:
    """An ASGI app (a class instance, so Starlette's Route treats it as one and not as a request handler)."""

    def __init__(self, app: Callable, limiter: RateLimiter, name: str, methods: set[str]):
        self.app, self.limiter, self.name, self.methods = app, limiter, name, methods

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] == "http" and scope.get("method") in self.methods:
            client = (scope.get("client") or ("", 0))[0]
            wait = self.limiter.allow(self.name, client)
            if wait:
                response = problem(429, "rate_limited", "Too many requests; try again shortly.",
                                   headers={"Retry-After": str(max(1, int(wait + 0.999)))})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


__all__ = ["RateLimiter", "WRITE_METHODS", "limited"]
