# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Logs and metrics, the cloud-native way: JSON logs to stdout (collected by Fluent Bit / the
platform), Prometheus metrics on a separate port that is not exposed publicly.

Redaction has two layers and both run on every line that leaves the process, in every format:

1. patterns (``redact``): things that *look* like Inkcast tokens, bearer headers and ``key=value`` secrets;
2. registered values (``libs.secrets.REDACTOR``): the exact credentials this process has loaded, in any
   encoding, wherever they ended up: a message, a field, a traceback, a library's error text.

Structured fields: pass ``extra={"event": "...", "post_id": ...}`` to any logger call and the JSON
formatter emits them as top-level keys. ``event`` names are a contract: every one used in code is in
``EVENTS`` (a test checks both directions), so alert rules and dashboards can rely on them.
``request_id`` is filled in automatically from the current request (see libs/http/middleware.py).
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import threading
import time
from collections import defaultdict
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from .secrets import REDACTOR

# The request id of the request being handled on this task/thread ("" outside a request).
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("inkcast_request_id", default="")


def current_request_id() -> str:
    return request_id_var.get()


# ---- the event catalogue --------------------------------------------------------------------
# Every `event` the code logs on purpose, with its severity and what on-call should know. Alert rules
# outside the app match on these names; a test fails on an event name that is not listed here.

EVENTS: dict[str, tuple[str, str]] = {
    "config.unsafe": ("critical", "The process refused to start (exit 78): the log line lists each problem."),
    "config.posture.warn": ("warning", "A soft security posture finding; fix it before production."),
    "secrets.loaded": ("info", "A credential was read for the first time (name and source, never the value)."),
    "secrets.changed": ("info", "A credential's value changed after a re-read: rotation picked up without a restart."),
    "secrets.status": ("info", "Startup summary of each credential: set or not, source, fingerprint."),
    "secrets.fetch.stale": ("warning", "A re-read failed; the last good value is served until INKCAST_SECRETS_MAX_STALE."),
    "secrets.fetch.failed": ("critical", "A credential could not be read and nothing is cached (or a forced refresh failed)."),
    "scheduler.tick.failed": ("critical", "A scheduler tick crashed; the next tick retries."),
    "publish.unconfirmed": ("warning", "A platform may or may not have published a post; it is never retried automatically."),
    "auth.login.refused": ("warning", "A sign-in was refused by the allow-list, role or password check."),
}

# Bearer secrets Inkcast mints or handles, and the usual ways secrets appear in text.
_SECRET_PATTERNS = [
    re.compile(r"\b(ink_pat_[A-Za-z0-9]+_)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(ic_(?:at|rt|code)_)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/\-]{8,}=*"),
    re.compile(r"(?i)\b((?:api[_-]?key|client_secret|password|access_token|refresh_token|code)=)[^&\s\"']+"),
]


def redact(text: str) -> str:
    """Both layers: pattern-based, then every registered secret value."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return REDACTOR.scrub(text)


class Redactor(logging.Filter):
    """Masks tokens and keys in the message of every record, whatever logger or library produced it.
    (The formatters scrub the whole rendered line again, tracebacks included.)"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


# LogRecord's own attributes; anything else on a record came from `extra=` and is emitted as a field.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}


def _extras(record: logging.LogRecord) -> dict:
    out = {}
    for key, value in record.__dict__.items():
        if key in _STANDARD or key.startswith("_"):
            continue
        if not isinstance(value, (str, int, float, bool, type(None))):
            value = str(value)
        out[key] = value
    return out


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = "inkcast"):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = current_request_id()
        if request_id:
            entry["request_id"] = request_id
        entry.update(_extras(record))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        line = json.dumps(entry, ensure_ascii=False)
        # The one deliberate exception: INKCAST_LOG_REVEAL_SECRETS lines (refused in production) keep their value.
        return line if getattr(record, "revealed", False) else redact(line)


class TextFormatter(logging.Formatter):
    """The development format. The whole rendered line, traceback included, goes through ``redact``."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = {k: v for k, v in _extras(record).items() if k != "revealed"}
        request_id = current_request_id()
        if request_id:
            extras = {"request_id": request_id, **extras}
        if extras:
            line += " · " + " ".join(f"{k}={v}" for k, v in extras.items())
        return line if getattr(record, "revealed", False) else redact(line)


def setup_logging(fmt: str, level: str = "INFO", service: str = "inkcast") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service) if fmt == "json" else TextFormatter(
        f"%(asctime)s %(levelname)s [{service}] %(name)s: %(message)s"))
    handler.addFilter(Redactor())
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
