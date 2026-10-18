# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Secret handling shared by every service.

* ``SecretValue``   wraps a secret so it can't leak through ``repr``/``str``/logs.
* ``SecretsProvider`` reads secrets from the environment or ``NAME_FILE`` mounts, caching them
  with a TTL plus jitter, one reader at a time (single-flight), and keeping the last good value
  if a re-read fails (stale-on-error) so a rotated Secret never takes a running service down.
* ``Sealer`` encrypts platform credentials at rest; ``token_hash``/``new_secret`` are how
  bearer secrets are minted and stored (only the SHA-256 hash is ever persisted).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import secrets
import threading
import time
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


class SecretValue:
    """A secret that prints as ``***``. Call ``reveal()`` at the one place it is really needed."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "SecretValue('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and secrets.compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        return hash(token_hash(self._value))


class SecretsProvider:
    """Read ``NAME`` or the file named by ``NAME_FILE``; cache for ``ttl`` seconds (±10% jitter)."""

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


class Sealer:
    """Encrypts platform credentials at rest."""

    def __init__(self, secret_key: str):
        key = hashlib.sha256(("inkcast-credentials:" + secret_key).encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, data: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def unseal(self, sealed: str) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(sealed.encode()))
        except InvalidToken as exc:
            raise RuntimeError(
                "Stored credentials can't be decrypted; INKCAST_SECRET_KEY changed. Reconnect the platform."
            ) from exc
