# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The in-process cache: TTL per key, a least-recently-used bound, and the same token bucket the
Redis adapter runs in Lua, so a limit behaves the same on a laptop and in a cluster."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from . import CacheBackend


class MemoryCache(CacheBackend):
    name = "memory"
    distributed = False

    def __init__(self, max_entries: int = 50_000, clock: Callable[[], float] = time.monotonic, metrics: Any = None):
        super().__init__(metrics)
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[Any, float | None]] = OrderedDict()  # key -> (value, expires_at)

    # ---- sync core (the rate limiter calls it without awaiting) --------------------

    def _get(self, key: str) -> Any:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expires = item
            if expires is not None and expires <= self._clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def _put(self, key: str, value: Any, ttl: float | None) -> None:
        with self._lock:
            self._data[key] = (value, self._clock() + ttl if ttl else None)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def bucket_sync(self, key: str, capacity: float, refill_per_second: float, cost: float = 1.0) -> float:
        now = self._clock()
        with self._lock:
            item = self._data.get(key)
            tokens, updated = item[0] if item and not (item[1] and item[1] <= now) else (capacity, now)
            tokens = min(capacity, tokens + (now - updated) * refill_per_second)
            wait = 0.0
            if tokens >= cost:
                tokens -= cost
            else:
                wait = (cost - tokens) / refill_per_second if refill_per_second > 0 else float("inf")
            ttl = (capacity / refill_per_second) if refill_per_second > 0 else None  # a full, idle bucket is forgotten
            self._data[key] = ((tokens, now), now + ttl if ttl else None)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
        return wait

    # ---- the async contract ----------------------------------------------------------

    async def get(self, key: str) -> str | None:
        value = self._get(key)
        return None if value is None else str(value)

    async def set(self, key: str, value: str, ttl: float | None = None, nx: bool = False) -> bool:
        if nx and self._get(key) is not None:
            return False
        self._put(key, value, ttl)
        return True

    async def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    async def incr(self, key: str, amount: int = 1, ttl: float | None = None) -> int:
        with self._lock:
            item = self._data.get(key)
            now = self._clock()
            if item is None or (item[1] is not None and item[1] <= now):
                value, expires = amount, (now + ttl if ttl else None)
            else:
                value, expires = int(item[0]) + amount, item[1]
            self._data[key] = (value, expires)
            self._data.move_to_end(key)
        return value

    async def token_bucket(self, key: str, capacity: float, refill_per_second: float, cost: float = 1.0) -> float:
        return self.bucket_sync(key, capacity, refill_per_second, cost)

    async def ping(self) -> bool:
        return True

    def __len__(self) -> int:
        return len(self._data)


__all__ = ["MemoryCache"]
