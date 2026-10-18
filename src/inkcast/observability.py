# SPDX-License-Identifier: PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Logs and metrics, the cloud-native way: JSON logs to stdout (collected by
Fluent Bit / the platform), Prometheus metrics on a separate port that is not
exposed publicly."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import defaultdict
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("platform", "post_id", "user_id", "status"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(fmt: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Never log request URLs with query strings at INFO (they can carry OAuth codes).
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class Metrics:
    """A tiny Prometheus registry: counters and callback gauges, no dependency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(dict)
        self._help: dict[str, tuple[str, str]] = {}
        self._gauges: dict[str, Callable[[], dict[tuple[tuple[str, str], ...], float]]] = {}

    def counter(self, name: str, help_text: str) -> None:
        self._help[name] = ("counter", help_text)

    def gauge(self, name: str, help_text: str, fn: Callable[[], dict[tuple[tuple[str, str], ...], float]]) -> None:
        self._help[name] = ("gauge", help_text)
        self._gauges[name] = fn

    def inc(self, name: str, amount: float = 1, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._counters[name][key] = self._counters[name].get(key, 0) + amount

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = {k: dict(v) for k, v in self._counters.items()}
        for name, (kind, help_text) in sorted(self._help.items()):
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
            try:
                series = counters.get(name, {}) if kind == "counter" else self._gauges[name]()
            except Exception:  # a broken gauge must not break scraping
                series = {}
            for labels, value in sorted(series.items()):
                label_str = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                lines.append(f"{name}{{{label_str}}} {value}" if label_str else f"{name} {value}")
        return "\n".join(lines) + "\n"

    def asgi(self) -> Starlette:
        async def metrics(_: Request) -> PlainTextResponse:
            return PlainTextResponse(self.render(), media_type="text/plain; version=0.0.4")
        return Starlette(routes=[Route("/metrics", metrics)])


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
