# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``CacheBackend`` on Redis or Valkey through the RESP client. The token bucket is one Lua script, so a
limit shared by many replicas is taken atomically; the script is loaded once (``SCRIPT LOAD``) and run by
hash (``EVALSHA``), falling back to ``EVAL`` after a server restart (``NOSCRIPT``)."""

from __future__ import annotations

import hashlib
from typing import Any

from ..secrets import SecretValue
from . import CacheBackend
from .resp import RespClient, RespError

# KEYS[1] = bucket; ARGV = capacity, refill per second, now (seconds), cost. Returns the wait as a string
# (Lua numbers would be truncated to integers in the reply). An idle bucket expires once it is full again.
TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local state = redis.call('HMGET', KEYS[1], 't', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then tokens = capacity; ts = now end
tokens = math.min(capacity, tokens + (now - ts) * rate)
local wait = 0
if tokens >= cost then tokens = tokens - cost else wait = (cost - tokens) / rate end
redis.call('HSET', KEYS[1], 't', tostring(tokens), 'ts', tostring(now))
if rate > 0 then redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000)) end
return tostring(wait)
"""
TOKEN_BUCKET_SHA = hashlib.sha1(TOKEN_BUCKET_LUA.encode()).hexdigest()


def _text(reply: Any) -> str | None:
    return reply.decode() if isinstance(reply, bytes) else reply


class RedisCache(CacheBackend):
    name = "redis"
    distributed = True

    def __init__(self, host: str, port: int = 6379, db: int = 0, *, tls: bool = False, username: str | None = None,
                 password: SecretValue | None = None, metrics: Any = None, clock=None, timeout: float = 2.0):
        super().__init__(metrics)
        import time

        self._clock = clock or time.time
        self.client = RespClient(host, port, tls=tls, timeout=timeout)
        if password:
            # The one place the cache credential leaves its wrapper: the AUTH command on (re)connect.
            secret = password.reveal() if isinstance(password, SecretValue) else str(password)
            self.client.on_connect.append(("AUTH", username, secret) if username else ("AUTH", secret))
        if db:
            self.client.on_connect.append(("SELECT", db))
        self._script_loaded = False

    async def _run(self, *args: Any, expected: str = "") -> Any:
        try:
            return await self.client.command(*args)
        except RespError as exc:  # a server error the caller handles (NOSCRIPT) is not a backend failure
            if not (expected and str(exc).startswith(expected)):
                self._error()
            raise
        except Exception:  # a lost connection or a timeout: counted, then the caller decides
            self._error()
            raise

    async def get(self, key: str) -> str | None:
        return _text(await self._run("GET", key))

    async def set(self, key: str, value: str, ttl: float | None = None, nx: bool = False) -> bool:
        args: list[Any] = ["SET", key, value]
        if ttl:
            args += ["PX", max(1, int(ttl * 1000))]
        if nx:
            args.append("NX")
        return await self._run(*args) is not None

    async def delete(self, key: str) -> bool:
        return int(await self._run("DEL", key)) > 0

    async def incr(self, key: str, amount: int = 1, ttl: float | None = None) -> int:
        value = int(await self._run("INCRBY", key, amount))
        if ttl and value == amount:  # just created: give it a lifetime
            await self._run("PEXPIRE", key, max(1, int(ttl * 1000)))
        return value

    async def token_bucket(self, key: str, capacity: float, refill_per_second: float, cost: float = 1.0) -> float:
        args = (1, key, capacity, refill_per_second, self._clock(), cost)
        if not self._script_loaded:
            await self._run("SCRIPT", "LOAD", TOKEN_BUCKET_LUA)
            self._script_loaded = True
        try:
            reply = await self._run("EVALSHA", TOKEN_BUCKET_SHA, *args, expected="NOSCRIPT")
        except RespError as exc:
            if not str(exc).startswith("NOSCRIPT"):
                raise
            self._script_loaded = False
            reply = await self._run("EVAL", TOKEN_BUCKET_LUA, *args)
        return float(_text(reply) or 0)

    async def ping(self) -> bool:
        try:
            return _text(await self._run("PING")) == "PONG"
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.close()


__all__ = ["RedisCache", "TOKEN_BUCKET_LUA", "TOKEN_BUCKET_SHA"]
