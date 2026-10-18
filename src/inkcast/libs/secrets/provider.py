# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""In-memory, rotation-aware secrets cache in front of a ``Fetcher``.

- Values live only in this process's memory, wrapped in ``SecretValue``; ``reveal()`` at the point of use.
- TTL with ±10% jitter, so a fleet does not re-read in lockstep.
- Single-flight: concurrent requests for a cold secret make one fetch; the rest wait and share it.
- Stale-on-error: if the source is briefly unreachable, keep serving the last good value, bounded
  by ``max_stale`` seconds, instead of failing requests.
- ``refresh(name, reason)``: consumers call this when the other side rejects the cached credential
  (it was rotated), then retry once.
- ``on_change(callback)``: told when a value changes (``SecretEvent``), e.g. to log, alert or rebuild a client.
- Every loaded value is registered with the ``Redactor`` *before* anything else can log it.
- ``status()``: metadata for the metrics port and dashboards. Never includes values; the
  fingerprint is an HMAC under a per-process random key, so it only shows "changed or not".

Reads are synchronous (files and loopback HTTP); from async code use ``aget``/``arefresh``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .fetchers import EnvFetcher, Fetched, Fetcher
from .mask import describe_keys, fingerprint
from .redactor import REDACTOR, Redactor
from .value import SecretValue

log = logging.getLogger(__name__)

STALE_RETRY_SECONDS = 30.0  # after a failed re-read, try again this soon (not a full TTL later)


class SecretNotFound(LookupError):
    """``require(name)``: no source has this secret."""


@dataclass(frozen=True)
class SecretEvent:
    name: str
    reason: str  # cold-start | ttl-expired | <reason given to refresh()>
    changed: bool
    fingerprint: str
    previous_fingerprint: str | None
    source: str
    version: str | None


@dataclass
class _Entry:
    value: str
    source: str
    version: str | None
    fingerprint: str
    fetched_at: float  # monotonic, for ages and TTLs
    loaded_at: str  # wall clock, ISO 8601, for people
    expires_at: float
    hits: int = 0
    fetches: int = 0
    last_reason: str = ""
    last_error: str | None = None
    changes: int = 0


@dataclass
class _Status:
    """What ``status()`` reports for one secret (a dataclass so the fields are documented and stable)."""

    name: str
    source: str
    version: str | None
    fingerprint: str
    length: int
    present: bool
    loaded_at: str
    age_seconds: int
    ttl_remaining_seconds: int
    cache_hits: int
    fetches: int
    changes: int
    last_reason: str
    last_error: str | None
    stale: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class SecretsProvider:
    """``SecretsProvider(fetcher, ttl=300, max_stale=3600)``. Without a fetcher: env + ``NAME_FILE``."""

    def __init__(self, fetcher: Fetcher | None = None, *, ttl: float = 300.0, max_stale: float = 3600.0,
                 environ: Mapping[str, str] | None = None, clock: Callable[[], float] = time.monotonic,
                 redactor: Redactor | None = None, reveal_in_logs: bool = False):
        self.fetcher = fetcher if fetcher is not None else EnvFetcher(environ)
        self.ttl = ttl
        self.max_stale = max_stale
        self._clock = clock
        self._redactor = redactor if redactor is not None else REDACTOR
        self._reveal = reveal_in_logs
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._listeners: list[Callable[[SecretEvent], None]] = []
        self._key = _secrets.token_bytes(32)  # fingerprints are only comparable within this process
        self._starting = True

    # ---- public API --------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self.fetcher.kind

    def get(self, name: str) -> SecretValue:
        """The secret, cached. Empty when no source has it (``Config.validate`` reports what is required)."""
        entry = self._entries.get(name)
        if entry and self._clock() < entry.expires_at:
            entry.hits += 1
            return SecretValue(entry.value)
        return self._load(name, force=False, reason="ttl-expired" if entry else "cold-start")

    def require(self, name: str) -> SecretValue:
        value = self.get(name)
        if not value:
            raise SecretNotFound(f"secret {name} is not set (no source has it)")
        return value

    def refresh(self, name: str, reason: str = "manual") -> SecretValue:
        """Bypass the cache, e.g. after the other side rejected the cached credential."""
        return self._load(name, force=True, reason=reason)

    async def aget(self, name: str) -> SecretValue:
        entry = self._entries.get(name)
        if entry and self._clock() < entry.expires_at:
            entry.hits += 1
            return SecretValue(entry.value)
        return await asyncio.to_thread(self.get, name)

    async def arefresh(self, name: str, reason: str = "manual") -> SecretValue:
        return await asyncio.to_thread(self.refresh, name, reason)

    def on_change(self, callback: Callable[[SecretEvent], None]) -> None:
        """Call ``callback(event)`` after every load whose value differs from the cached one."""
        self._listeners.append(callback)

    def startup_done(self) -> None:
        """After the first successful load: from now on a failed fetch is an error, not a startup blip."""
        self._starting = False

    def describe(self, name: str) -> dict[str, Any] | None:
        """Remote metadata (no value), when the source has any."""
        return self.fetcher.describe(name)

    def paths(self, name: str) -> list[str]:
        return self.fetcher.paths(name)

    def names(self) -> list[str]:
        return sorted(self._entries)

    def status(self) -> list[dict[str, Any]]:
        """Dashboard-safe metadata for every secret this process has asked for. Contains no secret material."""
        now = self._clock()
        out = []
        for name, e in sorted(self._entries.items()):
            out.append(_Status(
                name=name, source=e.source, version=e.version, fingerprint=e.fingerprint, length=len(e.value),
                present=bool(e.value), loaded_at=e.loaded_at, age_seconds=int(now - e.fetched_at),
                ttl_remaining_seconds=max(0, int(e.expires_at - now)), cache_hits=e.hits, fetches=e.fetches,
                changes=e.changes, last_reason=e.last_reason, last_error=e.last_error,
                stale=e.last_error is not None,
            ).__dict__)
        return out

    # ---- internals ------------------------------------------------------------------

    def _read(self, name: str, force: bool = False) -> Fetched | None:
        """One fetch from the source (kept as a method so tests can observe or stub it)."""
        return self.fetcher.fetch(name, force=force)

    def _load(self, name: str, *, force: bool, reason: str) -> SecretValue:
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        with lock:  # single-flight: one fetch per name; later callers find the fresh entry
            entry = self._entries.get(name)
            if entry and not force and self._clock() < entry.expires_at:
                entry.hits += 1
                return SecretValue(entry.value)
            return self._do_load(name, force=force, reason=reason)

    def _do_load(self, name: str, *, force: bool, reason: str) -> SecretValue:
        prev = self._entries.get(name)
        started = self._clock()
        try:
            fetched = self._read(name, True) if force else self._read(name)
        except Exception as exc:
            return self._failed(name, prev, exc, force=force, reason=reason)
        if fetched is None:
            fetched = Fetched("", "absent", None)
        self._redactor.register(fetched.value)  # BEFORE anything else can log it
        fp = fingerprint(fetched.value, self._key)
        changed = prev is not None and prev.fingerprint != fp
        now = self._clock()
        entry = _Entry(
            value=fetched.value, source=fetched.source, version=fetched.version, fingerprint=fp, fetched_at=now,
            loaded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            expires_at=now + self.ttl * random.uniform(0.9, 1.1), hits=prev.hits if prev else 0,
            fetches=(prev.fetches if prev else 0) + 1, last_reason=reason, last_error=None,
            changes=(prev.changes if prev else 0) + (1 if changed else 0),
        )
        self._entries[name] = entry
        ms = int((now - started) * 1000)
        if changed:
            log.info("Secret %s changed (%s, %s → %s, source %s)", name, reason, prev.fingerprint, fp, fetched.source,
                     extra={"event": "secrets.changed", "secret_name": name, "reason": reason, "ms": ms})
        elif prev is None and fetched.value:
            log.info("Secret %s loaded from %s", name, fetched.source,
                     extra={"event": "secrets.loaded", "secret_name": name, "reason": reason, "ms": ms})
        elif prev is None:
            log.debug("Secret %s is not set (no source has it)", name, extra={"secret_name": name, "reason": reason})
        self._log_consumed(name, entry, prev)
        if changed:
            event = SecretEvent(name, reason, True, fp, prev.fingerprint if prev else None, fetched.source, fetched.version)
            for listener in list(self._listeners):
                try:
                    listener(event)
                except Exception:  # a listener bug must not break the secret path
                    log.exception("A secrets on_change listener failed")
        return SecretValue(entry.value)

    def _failed(self, name: str, prev: _Entry | None, exc: Exception, *, force: bool, reason: str) -> SecretValue:
        age = self._clock() - prev.fetched_at if prev else float("inf")
        servable = not force and prev is not None and age < self.max_stale
        if prev:
            prev.last_error = f"{type(exc).__name__}: {exc}"
        if servable:
            log.warning("Could not re-read secret %s (%s); using the cached value (%ds old)", name, exc, int(age),
                        extra={"event": "secrets.fetch.stale", "secret_name": name, "age_seconds": int(age)})
            prev.expires_at = self._clock() + min(STALE_RETRY_SECONDS, self.ttl)
            return SecretValue(prev.value)
        if self._starting and prev is None:
            log.debug("Secret %s could not be read during startup (%s)", name, exc)
        else:
            log.error("Could not read secret %s (%s); cached=%s", name, exc, prev is not None,
                      extra={"event": "secrets.fetch.failed", "secret_name": name, "cached": prev is not None})
        raise exc

    def _log_consumed(self, name: str, entry: _Entry, prev: _Entry | None) -> None:
        if not log.isEnabledFor(logging.DEBUG):
            return
        fields = {"secret_name": name, "source": entry.source, "version": entry.version,
                  "fingerprint": f"{prev.fingerprint} -> {entry.fingerprint}" if prev and prev.fingerprint != entry.fingerprint
                  else entry.fingerprint}
        log.debug("Secret consumed: %s", " ".join(describe_keys({name: entry.value, **{k: v for k, v in fields.items()
                                                                                        if k != "secret_name"}}, self._key)),
                  extra=fields)
        if self._reveal:  # INKCAST_LOG_REVEAL_SECRETS=true, refused in production: the ONE unredacted line
            log.debug("Secret consumed (value revealed): %s=%s", name, entry.value,
                      extra={**fields, "revealed": True, "warning": "secret value NOT masked (INKCAST_LOG_REVEAL_SECRETS)"})


__all__ = ["STALE_RETRY_SECONDS", "SecretEvent", "SecretNotFound", "SecretsProvider"]
