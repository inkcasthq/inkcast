# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The per-process context every service is built from: config, storage, HTTP client, plug-ins, metrics.

One process builds one ``Context``, however many services it runs (``All`` runs them all on it).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx2

from .. import __version__
from .auth import TokenVerifier
from .authz import PolicyEngine, build_engine
from .cache import CacheBackend, build_cache
from .config import Config
from .db import Store
from .db.identity import SYNC_RECONCILED
from .http.ratelimit import RateLimiter
from .observability import LAG_BUCKETS, Metrics
from .pipeline import Publisher
from .platforms import Platform, load_platforms

log = logging.getLogger("inkcast.runtime")


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
    cache: CacheBackend | None = None  # libs/cache: in-process, or Redis/Valkey shared by every replica
    authz: PolicyEngine | None = None  # libs/authz: the policy engine INKCAST_AUTHZ_ENGINE picked (None in a Gateway)

    @property
    def runs_scheduler(self) -> bool:
        return "SchedulerWorker" in self.services


def build_metrics(store: Store | None, registry: dict[str, Platform], config: Config,
                  clock: Callable[[], float] = time.time) -> Metrics:
    m = Metrics()
    m.gauge("inkcast_build_info", "Inkcast version and installed platform plug-ins.",
            lambda: {(("platforms", ",".join(registry)), ("version", __version__)): 1})
    m.counter("inkcast_publish_total", "Publish attempts by platform and outcome.")
    m.counter("inkcast_scheduler_ticks_total", "Completed scheduler ticks.")
    m.counter("inkcast_sync_total", "Git publishing sync results by platform and action.")
    m.counter("inkcast_gateway_requests_total", "Requests proxied by the built-in gateway, by upstream and status class.")
    m.histogram("inkcast_http_request_duration_seconds",
                "Request duration by route template, method and status class (RED: rate, errors, duration).")
    m.histogram("inkcast_publish_duration_seconds", "Time one platform publish call took, by platform.")
    m.histogram("inkcast_publish_lag_seconds",
                "How late a scheduled post was claimed after its run_at (schedule lag), by platform.", LAG_BUCKETS)
    m.counter("inkcast_ratelimit_total", "Rate-limit decisions on unauthenticated writes, by limit and decision.")
    m.counter("inkcast_identity_sync_total", "Keycloak user sync passes, by result (ok, error).")
    m.counter("inkcast_identity_sync_errors_total", "Keycloak user sync failures the pass went on past, by stage (person: "
              "Keycloak answered an error about one person, who is read again at the next pass or reconcile).")
    m.counter("inkcast_identity_sync_ended_early_total", "Keycloak user sync passes that ended early, a success for what they "
              "did, because their budget of reads about people with no answer was used, by the stage they ended in "
              "(events, admin-events, reconcile).")
    m.counter("inkcast_identity_users_disabled_total", "People disabled in Inkcast because Keycloak disabled or deleted them, by reason.")
    m.counter("inkcast_identity_users_deleted_total", "People erased from Inkcast once their account was gone, by how Inkcast learnt of it (return, event, reconcile).")
    m.counter("inkcast_cache_requests_total", "Cache reads by backend and result (hit or miss): the hit ratio.")
    m.counter("inkcast_cache_errors_total", "Cache backend failures by backend (a request fell back or failed).")
    m.counter("inkcast_outbox_events_total", "Domain events handed to event sinks, by sink and result (handled, failed).")
    m.counter("inkcast_kpi_events_total", "Domain events folded into KPI rollups, by result (applied, duplicate).")
    m.counter("inkcast_authz_checks_total", "Authorization checks by relation and result (allowed, denied, error).")
    m.histogram("inkcast_authz_duration_seconds", "Time one policy engine call took, by operation and engine.")
    if store is None:
        return m
    m.gauge("inkcast_posts", "Posts by status.", lambda: {(("status", k),): v for k, v in store.post_counts().items()})
    m.gauge("inkcast_posts_due", "Scheduled posts that are due now (queue depth).",
            lambda: {(): store.due_count(time.time())})
    m.gauge("inkcast_posts_overdue", "Scheduled posts past due by more than INKCAST_OVERDUE_GRACE (scheduler behind).",
            lambda: {(): store.overdue_count(time.time(), config.overdue_grace_seconds)})
    m.gauge("inkcast_posts_oldest_due_age_seconds",
            "Age of the oldest due post still waiting (0 when the queue is empty): the queue's head-of-line lag.",
            lambda: {(): store.oldest_due_age(time.time())})
    m.gauge("inkcast_posts_recovering_oldest_age_seconds",
            "How long the oldest post whose send was interrupted has been 'recovering', the platform refusing to say "
            "whether it arrived (0 when none is). InkcastPostRecoveringTooLong fires past an hour; "
            "INKCAST_PUBLISH_RECOVERY_MAX_HOURS ends it unconfirmed.",
            lambda: {(): store.oldest_recovering_age(time.time())})
    m.gauge("inkcast_outbox_pending", "Domain events a consumer has not handled yet, by consumer.",
            lambda: {(("consumer", c),): n for c, (n, _) in store.outbox_lag().items()})
    m.gauge("inkcast_outbox_oldest_age_seconds", "Age of the oldest unhandled event, by consumer (consumer lag).",
            lambda: {(("consumer", c),): age for c, (_, age) in store.outbox_lag().items()})
    # Read from the database, so it is 1 from the moment a pass trips until an operator resolves it, restarts included.
    m.gauge("inkcast_identity_sync_breaker_open", "1 while the Keycloak user sync holds a pass that found too many people "
            "gone or disabled at once, for an operator to confirm or dismiss (inkcast identity); 0 otherwise.",
            lambda: {(): 1 if config.keycloak_breaker and store.pending_breaker_trip() else 0})   # off holds nobody
    # Also from the database (#180): the time the sync last reconciled everyone it could, written by whichever process
    # runs it, so the age is the realm's, survives a restart or a crash loop, and before the first is counted from
    # this process's start. No series where the sync is off, so the alert on it cannot fire there.
    started, synced = clock(), bool(config.keycloak_sync_client_id)
    m.gauge("inkcast_identity_sync_reconcile_age_seconds", "Seconds since the Keycloak user sync last reconciled everyone "
            "it could (every person not known to hang was read); since the process started until it first has. "
            "InkcastIdentitySyncStale fires past three reconcile intervals.",
            lambda: {(): max(0.0, clock() - float(store.get_sync_state(SYNC_RECONCILED) or started))} if synced else {})
    m.gauge("inkcast_identity_sync_reconcile_interval_seconds", "INKCAST_KEYCLOAK_RECONCILE_INTERVAL as configured, for the "
            "alert on the reconcile age.", lambda: {(): float(config.keycloak_reconcile_seconds)} if synced else {})
    return m


def open_store(config: Config) -> Store:
    """The process's store, with the erasures re-applied before anything serves (issue #94): a database restored from
    a backup taken before a person's erasure holds their rows again; the audit row the restore carried over says who.
    Every service process runs this once at start-up; it is idempotent, a no-op when nothing matches, and not a new
    deletion (logged with the id and the count, never counted in inkcast_identity_users_deleted_total)."""
    store = Store(config.database_path, config.secret_key)
    for user_id, counts in store.reapply_erasures().items():
        rows = sum(counts.values())
        log.warning("Inkcast user %s was erased before and came back with a restore: erased %d rows again", user_id, rows,
                    extra={"event": "identity.erasure.reapplied", "user_id": user_id, "rows": rows})
    return store


def build_context(config: Config, transport: httpx2.AsyncBaseTransport | None = None,
                  services: tuple[str, ...] = (), storage: bool = True) -> Context:
    store = open_store(config) if storage else None
    http = httpx2.AsyncClient(timeout=httpx2.Timeout(30.0, connect=10.0), transport=transport, follow_redirects=False)
    registry = load_platforms(http, config.platform_urls)
    metrics = build_metrics(store, registry, config)
    publisher = Publisher(store, registry, metrics, retry=config.publish_retry,
                          recovery_max=config.publish_recovery_max_seconds) if store else None
    verifier = TokenVerifier(store) if store else None
    cache = build_cache(config.cache_url, config.cache_password, metrics)
    authz = build_engine(config, store, http, metrics) if store else None
    return Context(config, store, http, registry, metrics, publisher, verifier, services=services,
                   limiter=RateLimiter(config.rate_limit_per_minute, backend=cache, metrics=metrics), cache=cache,
                   authz=authz)
