# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``SecretsProvider``: reads ``NAME`` or the file named by ``NAME_FILE``, caching for ``ttl`` seconds
(±10% jitter), one reader at a time per name (single-flight), and keeping the last good value
when a re-read fails (stale-on-error)."""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Callable

from .value import SecretValue

log = logging.getLogger(__name__)


class SecretsProvider:
    def __init__(self, ttl: float = 300.0, environ: dict[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._env = environ if environ is not None else os.environ
        self._clock = clock
        self._cache: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _read(self, name: str) -> str:
        path = self._env.get(f"{name}_FILE", "").strip()
        if path:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        return self._env.get(name, "").strip()

    def get(self, name: str) -> SecretValue:
        cached = self._cache.get(name)
        if cached and self._clock() < cached[1]:
            return SecretValue(cached[0])
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        with lock:  # single-flight: one re-read per name, others wait and reuse it
            cached = self._cache.get(name)
            if cached and self._clock() < cached[1]:
                return SecretValue(cached[0])
            try:
                value = self._read(name)
            except OSError as exc:
                if cached:  # stale-on-error: keep serving the last good value
                    log.warning("Could not re-read secret %s (%s); using the cached value", name, exc)
                    self._cache[name] = (cached[0], self._clock() + min(30.0, self.ttl))
                    return SecretValue(cached[0])
                raise
            self._cache[name] = (value, self._clock() + self.ttl * random.uniform(0.9, 1.1))
            return SecretValue(value)


__all__ = ["SecretsProvider"]
