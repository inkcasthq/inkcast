# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Caching and coordination, behind one small interface.

``CacheBackend`` is the seam every service uses for short-lived shared state: cached lookups, distributed
rate limits (a token bucket), locks, idempotency markers and, later, web sessions (the E4 BFF). Two
adapters ship:

* ``MemoryCache`` (``INKCAST_CACHE_URL`` unset): per process, with TTLs, an LRU bound and single-flight loads.
  Right for ``All`` on one node, laptops and CI.
* ``RedisCache`` (``INKCAST_CACHE_URL=redis://host:6379/0`` or ``rediss://``): shared by every replica. It
  speaks RESP over asyncio streams (``resp.py``); no client library is needed, which matters for air-gapped
  installs where every dependency is mirrored and licence-checked. Valkey (BSD-3, Linux Foundation) and Redis
  speak the same protocol; the commands used (GET, SET EX NX, INCRBY, EXPIRE, DEL, EVAL) are in both.

Keys are namespaced by the caller (``rl:register:1.2.3.4``, ``idem:<user>:<key>``). A backend never raises
into a request path for a *coordination* use: the rate limiter falls back to its in-process bucket and
counts the failure (``inkcast_cache_errors_total``), so a cache outage degrades to single-node behaviour
instead of an outage.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable


class CacheBackend(ABC):
    """What a cache must do. Values are strings (callers encode JSON themselves); TTLs are seconds."""

    name: str = "none"
    distributed: bool = False  # True when every replica sees the same keys

    def __init__(self, metrics: Any = None):
        self.metrics = metrics
        self._inflight: dict[str, asyncio.Future] = {}

    # ---- the contract ------------------------------------------------------------

    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str, ttl: float | None = None, nx: bool = False) -> bool:
        """Store ``value``; with ``nx`` only when the key is absent (a lock or an idempotency claim). True if stored."""

    @abstractmethod
    async def delete(self, key: str) -> bool: ...

    @abstractmethod
    async def incr(self, key: str, amount: int = 1, ttl: float | None = None) -> int:
        """Atomic add; ``ttl`` is set when the key is created (a fixed-window counter)."""

    @abstractmethod
    async def token_bucket(self, key: str, capacity: float, refill_per_second: float, cost: float = 1.0) -> float:
        """Take ``cost`` tokens from the bucket at ``key``. 0.0 when allowed, else the seconds until it would be."""

    @abstractmethod
    async def ping(self) -> bool: ...

    async def close(self) -> None:  # pragma: no cover - adapters that hold nothing
        return None

    # ---- shared behaviour ----------------------------------------------------------

    async def get_or_load(self, key: str, loader: Callable[[], Awaitable[str]], ttl: float | None = None) -> str:
        """Read-through with single flight: concurrent misses in this process run ``loader`` once."""
        value = await self.get(key)
        if value is not None:
            self._count("hit")
            return value
        self._count("miss")
        pending = self._inflight.get(key)
        if pending is not None:
            return await pending
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value = await loader()
            await self.set(key, value, ttl)
            future.set_result(value)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)
            if not future.done():  # pragma: no cover - defensive
                future.cancel()

    def _count(self, result: str) -> None:
        if self.metrics is not None:
            self.metrics.inc("inkcast_cache_requests_total", backend=self.name, result=result)

    def _error(self) -> None:
        if self.metrics is not None:
            self.metrics.inc("inkcast_cache_errors_total", backend=self.name)


def parse_cache_url(url: str) -> dict[str, Any]:
    """``redis://host:6379/2`` or ``rediss://host`` → connection settings. A password in the URL is refused:
    it would land in process listings and logs; use ``INKCAST_CACHE_PASSWORD`` (or ``_FILE``)."""
    from urllib.parse import urlparse

    parts = urlparse(url)
    if parts.scheme not in ("redis", "rediss", "valkey", "valkeys"):
        raise ValueError(f"INKCAST_CACHE_URL must start with redis:// or rediss:// (got {parts.scheme!r}).")
    if parts.password:
        raise ValueError("INKCAST_CACHE_URL must not carry a password; set INKCAST_CACHE_PASSWORD (or _FILE).")
    db = parts.path.strip("/")
    if db and not db.isdigit():
        raise ValueError(f"INKCAST_CACHE_URL database must be a number (got {db!r}).")
    return {"host": parts.hostname or "localhost", "port": parts.port or 6379, "db": int(db or 0),
            "tls": parts.scheme in ("rediss", "valkeys"), "username": parts.username or None}


def build_cache(url: str, password: Any = None, metrics: Any = None) -> CacheBackend:
    """The backend for this process: Redis/Valkey when ``url`` is set, else in-memory."""
    from .memory import MemoryCache

    if not url:
        return MemoryCache(metrics=metrics)
    from .redis import RedisCache

    return RedisCache(**parse_cache_url(url), password=password, metrics=metrics)


__all__ = ["CacheBackend", "build_cache", "parse_cache_url"]
