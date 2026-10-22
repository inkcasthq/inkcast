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
    "http.request": ("info", "One request: method, path (no query string), status, duration, request id, client."),
    "http.request.rejected": ("warning", "A request was refused before a handler ran (body too large)."),
    "gateway.upstream.unreachable": ("critical", "The built-in Gateway could not reach a service."),
    "scheduler.tick.failed": ("critical", "A scheduler tick crashed; the next tick retries."),
    "publish.unconfirmed": ("warning", "A platform may or may not have published a post; it is never retried automatically."),
    "publish.recovered": ("warning", "A post left 'publishing' by a restart was resolved: confirmed, rescheduled or unconfirmed."),
    "auth.login.refused": ("warning", "A sign-in was refused by the allow-list, role or password check."),
    "cache.backend.unavailable": ("warning", "Redis/Valkey did not answer; rate limits are per replica until it is back."),
    "outbox.consumer.failed": ("warning", "An event sink failed on a batch; the cursor stayed and the batch is retried."),
    "identity.user.disabled": ("warning", "Keycloak disabled or deleted a person: their Inkcast tokens and PATs were cut off."),
    "identity.user.deleted": ("warning", "A person's account is gone (deleted by them, or in Keycloak and past the grace "
                                         "period): every row of theirs was erased (ids and counts only)."),
    "identity.erasure.reapplied": ("warning", "A restore brought back rows of a person erased before; they were erased again at "
                                              "start-up, before anything served (id and count only; not a new deletion)."),
    "identity.sync.failed": ("warning", "A Keycloak user sync pass failed (service account refused, Keycloak down); retried."),
    "identity.sync.person.failed": ("warning", "Keycloak answered an error about one person (Inkcast user id, read and status "
                                               "only); nothing was done for them, the pass went on, they are read again."),
    "identity.sync.breaker.tripped": ("critical", "A reconcile found too many people gone from Keycloak at once (pass id and "
                                                  "counts); nobody is cut off or erased until an operator confirms or "
                                                  "dismisses the pass with inkcast identity."),
    "identity.sync.breaker.held": ("warning", "The breaker is still open (pass id, how many people are held); every pass "
                                              "says so until the pass is resolved."),
    "identity.sync.breaker.confirmed": ("warning", "An operator confirmed a pass (audit: pass id, who, how many applied and "
                                                   "skipped): its people are cut off and their grace period starts."),
    "identity.sync.breaker.dismissed": ("warning", "An operator dismissed a pass (audit: pass id, who): nothing was done; "
                                                   "the next reconcile reads everyone again."),
    "identity.sync.breaker.refused": ("warning", "An operator's confirm or dismiss did nothing: no such pass, or it was "
                                                 "resolved already (pass id only)."),
    "authz.denied": ("warning", "A permission check said no (user, relation and object ids; never a token or a name)."),
    "authz.error": ("critical", "The policy engine could not answer (OpenFGA down, bad model id); the caller failed closed (503)."),
    "backup.done": ("info", "A database backup was taken, checked and kept (name, size, rows)."),
    "backup.failed": ("critical", "A database backup failed; nothing was removed. The alert fires when none succeeds for 26 h."),
    "restore.done": ("warning", "The database was replaced by a backup; the previous one was kept beside it."),
    "restore.erasures.carried": ("info", "The erasure records of the replaced database went into the restored copy, so the "
                                         "server erases those people again at start-up (count only)."),
    "restore.refused": ("warning", "A restore did not run: the backup failed its checks, or the server was still running."),
}

# Bearer secrets Inkcast mints or handles, and the usual ways secrets appear in text.
_SECRET_PATTERNS = [
    re.compile(r"\b(ink_pat_[A-Za-z0-9]+_)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(ic_(?:at|rt|code)_)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(/connect/)[A-Za-z0-9_\-]{8,}"),  # one-time connect tickets travel in the path (access log)
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


# ---- route labels for RED metrics ----------------------------------------------------------
# Prometheus labels must come from a closed set. A request path is not one (ids, tickets, typos), so the
# access metrics carry the *route template* instead. Every public route is listed here; a path that matches
# none is labelled "other". tests/test_libs.py checks that every path in the OpenAPI document maps to itself.

_ROUTE_TEMPLATES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile("^" + re.sub(r"\{[a-z_]+\}", "[^/]+", template) + "$"), template) for template in (
        "/mcp",
        "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        "/authorize", "/token", "/register", "/revoke",
        "/auth/login", "/auth/callback", "/auth/consent", "/auth/logout",
        "/tokens/new",
        "/account/delete/{ticket}", "/account/return",
        "/connect/{ticket}",
        "/api", "/api/docs", "/api/redoc", "/api/openapi.json", "/api/static/{asset}",
        "/api/v1/me",
        "/api/v1/tokens", "/api/v1/tokens/{token_id}", "/api/v1/account/deletion-link",
        "/api/v1/platforms", "/api/v1/platforms/{platform_id}/connect-link",
        "/api/v1/platforms/{platform_id}/connection", "/api/v1/platforms/{platform_id}/destinations",
        "/api/v1/posts", "/api/v1/posts/{post_id}/cancel", "/api/v1/preview",
        "/api/v1/stories:sync", "/api/v1/ledger",
        "/api/v1/insights/summary", "/api/v1/insights/kpis",
        "/livez", "/readyz", "/healthz", "/metrics",
    )
)
_PREFIXES = ("/publishing-api", "/identity-service", "/connector-service", "/insights-service", "/gateway")


def route_label(path: str) -> str:
    """The route template a path belongs to (``/api/v1/posts/{post_id}/cancel``), or ``other``. A service
    prefix (``INKCAST_PATH_PREFIX=auto``) is stripped first, so the label is the same in every topology."""
    for prefix in _PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            path = path[len(prefix):] or "/"
            break
    for pattern, template in _ROUTE_TEMPLATES:
        if pattern.match(path):
            return template
    return "other"


# Buckets for request durations (seconds): sub-10 ms probes up to the 30 s platform timeout.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
# Buckets for schedule lag (seconds): a tick is 20 s, the overdue grace 600 s.
LAG_BUCKETS = (1.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0)

Labels = tuple[tuple[str, str], ...]


class Metrics:
    """A tiny Prometheus registry: counters, histograms and callback gauges, no dependency.

    It is the ``MetricsExporter`` seam: the text format is rendered by ``render()``; an OTLP exporter can read
    the same series through ``snapshot()`` later without touching the call sites.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[Labels, float]] = defaultdict(dict)
        self._help: dict[str, tuple[str, str]] = {}
        self._gauges: dict[str, Callable[[], dict[Labels, float]]] = {}
        self._buckets: dict[str, tuple[float, ...]] = {}
        # histogram name -> labels -> (per-bucket counts, sum, count)
        self._hist: dict[str, dict[Labels, tuple[list[int], float, int]]] = defaultdict(dict)

    def counter(self, name: str, help_text: str) -> None:
        self._help[name] = ("counter", help_text)

    def gauge(self, name: str, help_text: str, fn: Callable[[], dict[Labels, float]]) -> None:
        self._help[name] = ("gauge", help_text)
        self._gauges[name] = fn

    def histogram(self, name: str, help_text: str, buckets: tuple[float, ...] = LATENCY_BUCKETS) -> None:
        self._help[name] = ("histogram", help_text)
        self._buckets[name] = tuple(sorted(buckets))

    def inc(self, name: str, amount: float = 1, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._counters[name][key] = self._counters[name].get(key, 0) + amount

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record one observation in a histogram registered with ``histogram()``."""
        buckets = self._buckets.get(name)
        if buckets is None:
            return  # an unregistered histogram is a programming error, but never a failed request
        key = tuple(sorted(labels.items()))
        with self._lock:
            counts, total, n = self._hist[name].get(key) or ([0] * len(buckets), 0.0, 0)
            for i, bound in enumerate(buckets):
                if value <= bound:
                    counts[i] += 1
            self._hist[name][key] = (counts, total + value, n + 1)

    def snapshot(self) -> dict[str, dict[Labels, float]]:
        """Counters and gauges as plain numbers (histograms are exposed through ``render``)."""
        with self._lock:
            out = {k: dict(v) for k, v in self._counters.items()}
        for name, fn in self._gauges.items():
            try:
                out[name] = fn()
            except Exception:
                out[name] = {}
        return out

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = {k: dict(v) for k, v in self._counters.items()}
            hist = {k: {lab: (list(c), s, n) for lab, (c, s, n) in v.items()} for k, v in self._hist.items()}
        for name, (kind, help_text) in sorted(self._help.items()):
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
            if kind == "histogram":
                lines += self._render_histogram(name, hist.get(name, {}))
                continue
            try:
                series = counters.get(name, {}) if kind == "counter" else self._gauges[name]()
            except Exception:  # a broken gauge must not break scraping
                series = {}
            for labels, value in sorted(series.items()):
                lines.append(_sample(name, labels, value))
        return "\n".join(lines) + "\n"

    def _render_histogram(self, name: str, series: dict[Labels, tuple[list[int], float, int]]) -> list[str]:
        out: list[str] = []
        for labels, (counts, total, n) in sorted(series.items()):
            for bound, count in zip(self._buckets[name], counts):
                out.append(_sample(f"{name}_bucket", (*labels, ("le", _fmt(bound))), count))
            out.append(_sample(f"{name}_bucket", (*labels, ("le", "+Inf")), n))
            out.append(_sample(f"{name}_sum", labels, total))
            out.append(_sample(f"{name}_count", labels, n))
        return out

    def asgi(self) -> Starlette:
        async def metrics(_: Request) -> PlainTextResponse:
            return PlainTextResponse(self.render(), media_type="text/plain; version=0.0.4")
        return Starlette(routes=[Route("/metrics", metrics)])


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(value)


def _sample(name: str, labels: Labels, value: float) -> str:
    label_str = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return f"{name}{{{label_str}}} {value}" if label_str else f"{name} {value}"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
