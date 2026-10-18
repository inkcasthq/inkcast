# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A minimal RESP2 client over asyncio streams: what Redis and Valkey speak.

Why not a client library: the sandbox and air-gapped clusters can't fetch one, every dependency is one
more licence and mirror entry, and Inkcast needs seven commands. RESP2 is a few dozen lines to encode and
decode, and ``tests/fakes.py`` carries a fake server that speaks it, so the adapter is tested end to end
without a Redis process.

One connection per client, one command at a time (a lock serialises callers; the volumes here are small).
A broken connection is dropped and the next command reconnects once. Errors from the server are raised
as ``RespError`` with the server's text (``NOSCRIPT``, ``WRONGTYPE``...).
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

Reply = Any  # bytes | int | None | list[Reply]


class RespError(Exception):
    """An error reply from the server (``-ERR ...``), or a protocol problem."""


def encode(*args: str | bytes | int | float) -> bytes:
    out = [b"*%d\r\n" % len(args)]
    for arg in args:
        if isinstance(arg, bytes):
            data = arg
        elif isinstance(arg, float):
            data = repr(arg).encode()
        else:
            data = str(arg).encode()
        out.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(out)


async def read_reply(reader: asyncio.StreamReader) -> Reply:
    line = await reader.readuntil(b"\r\n")
    kind, body = line[:1], line[1:-2]
    if kind == b"+":
        return body
    if kind == b"-":
        raise RespError(body.decode(errors="replace"))
    if kind == b":":
        return int(body)
    if kind == b"$":
        length = int(body)
        if length < 0:
            return None
        data = await reader.readexactly(length + 2)
        return data[:-2]
    if kind == b"*":
        count = int(body)
        if count < 0:
            return None
        return [await read_reply(reader) for _ in range(count)]
    raise RespError(f"unexpected reply type {kind!r}")


class RespClient:
    def __init__(self, host: str, port: int, *, tls: bool = False, timeout: float = 2.0):
        self.host, self.port, self.tls, self.timeout = host, port, tls, timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock: asyncio.Lock | None = None
        self.on_connect: list[tuple[str | bytes | int, ...]] = []  # commands run after every (re)connect: AUTH, SELECT

    async def _connect(self) -> None:
        context = ssl.create_default_context() if self.tls else None
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=context), self.timeout)
        for command in self.on_connect:
            await self._send(*command)

    async def _send(self, *args: str | bytes | int | float) -> Reply:
        assert self._writer is not None and self._reader is not None
        self._writer.write(encode(*args))
        await asyncio.wait_for(self._writer.drain(), self.timeout)
        return await asyncio.wait_for(read_reply(self._reader), self.timeout)

    async def command(self, *args: str | bytes | int | float) -> Reply:
        """Run one command; reconnects once if the connection was lost."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            for attempt in (1, 2):
                try:
                    if self._writer is None:
                        await self._connect()
                    return await self._send(*args)
                except RespError:
                    raise
                except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                    await self._drop()
                    if attempt == 2:
                        raise
            raise RuntimeError("unreachable")  # pragma: no cover

    async def _drop(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # the peer is gone; nothing to wait for
                pass

    async def close(self) -> None:
        await self._drop()


__all__ = ["RespClient", "RespError", "encode", "read_reply"]
