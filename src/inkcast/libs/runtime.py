# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The per-process context every service is built from: config, storage, HTTP client, plug-ins, metrics.

One process builds one ``Context``, however many services it runs (``All`` runs them all on it).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx2

from .. import __version__
from .auth import TokenVerifier
from .config import Config
from .db import Store
from .http.ratelimit import RateLimiter
from .observability import Metrics
from .pipeline import Publisher
from .platforms import Platform, load_platforms


@dataclass
class Context:
    config: Config
    store: Store | None  # None in a process that needs no storage (a Gateway on its own)
    http: httpx2.AsyncClient
    platforms: dict[str, Platform]
    metrics: Metrics
    publisher: Publisher | None
    verifier: TokenVerifier | None
    services: tuple[str, ...] = ()  # CamelCase names running in this process
    extras: dict = field(default_factory=dict)  # per-service objects (e.g. the MCP server)
    limiter: RateLimiter = field(default_factory=RateLimiter)  # for the endpoints anyone can call (libs/http/ratelimit)

    @property
    def runs_scheduler(self) -> bool:
        return "SchedulerWorker" in self.services


def build_metrics(store: Store | None, registry: dict[str, Platform], config: Config) -> Metrics:
    m = Metrics()
    m.gauge("inkcast_build_info", "Inkcast version and installed platform plug-ins.",
            lambda: {(("platforms", ",".join(registry)), ("version", __version__)): 1})
    m.counter("inkcast_publish_total", "Publish attempts by platform and outcome.")
    m.counter("inkcast_scheduler_ticks_total", "Completed scheduler ticks.")
    m.counter("inkcast_sync_total", "Git publishing sync results by platform and action.")
    m.counter("inkcast_gateway_requests_total", "Requests proxied by the built-in gateway, by upstream and status class.")
    if store is None:
        return m
    m.gauge("inkcast_posts", "Posts by status.", lambda: {(("status", k),): v for k, v in store.post_counts().items()})
    m.gauge("inkcast_posts_due", "Scheduled posts that are due now (queue depth).",
            lambda: {(): store.due_count(time.time())})
    m.gauge("inkcast_posts_overdue", "Scheduled posts past due by more than INKCAST_OVERDUE_GRACE (scheduler behind).",
            lambda: {(): store.overdue_count(time.time(), config.overdue_grace_seconds)})
    return m


def build_context(config: Config, transport: httpx2.AsyncBaseTransport | None = None,
                  services: tuple[str, ...] = (), storage: bool = True) -> Context:
    store = Store(config.database_path, config.secret_key) if storage else None
    http = httpx2.AsyncClient(timeout=httpx2.Timeout(30.0, connect=10.0), transport=transport, follow_redirects=False)
    registry = load_platforms(http, config.platform_urls)
    metrics = build_metrics(store, registry, config)
    publisher = Publisher(store, registry, metrics) if store else None
    verifier = TokenVerifier(store) if store else None
    return Context(config, store, http, registry, metrics, publisher, verifier, services=services,
                   limiter=RateLimiter(config.rate_limit_per_minute))
