# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Logs and metrics, the cloud-native way: JSON logs to stdout (collected by Fluent Bit / the
platform), Prometheus metrics on a separate port that is not exposed publicly.

Redaction has two layers and both run on every line that leaves the process, in every format:

1. patterns (``redact``): things that *look* like Inkcast tokens, bearer headers and ``key=value`` secrets, and the
   one-time tickets that travel in a path, derived from the route templates (``ROUTE_TEMPLATES``);
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
from typing import Callable, Iterable

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
    "scheduler.housekeeping.failed": ("warning", "Housekeeping crashed (the outbox cursor sync, a purge or the orphan "
                                                 "sweep); the ticks go on, and it is tried again HOUSEKEEPING_EVERY later."),
    "publish.unconfirmed": ("warning", "A platform may or may not have published a post; it is never retried automatically."),
    "publish.recovered": ("warning", "A post left 'publishing' by a restart was resolved: confirmed, rescheduled or unconfirmed."),
    "publish.recovery.deferred": ("warning", "A post left 'publishing' by a restart could not be checked yet (the platform "
                                             "rate-limited or unreachable); it stays 'recovering' and is checked again."),
    "publish.recovery.capped": ("warning", "A post 'recovering' for INKCAST_PUBLISH_RECOVERY_MAX_HOURS, the platform never "
                                           "saying whether it arrived, is unconfirmed for a person: never sent late on its own."),
    "publish.recovery.failed": ("error", "One post's recovery could not be made (its credentials sealed under a previous "
                                         "INKCAST_SECRET_KEY, a payload this version can't read; post id and platform, with "
                                         "the traceback); it stays 'recovering' for the next stuck pass, and the pass went "
                                         "on to the rows after it. Counted as inkcast_publish_total{status=\"recovery_failed\"}."),
    "publish.second_copy": ("warning", "A post may be on its platform twice: a send taken over as stuck succeeded after "
                                       "another worker re-sent it, or after a lookup recorded another post. The post's "
                                       "error names both copies, or the attempt that may have made one; a person checks "
                                       "the platform, keeps one and removes the other."),
    "publish.edited_meanwhile": ("warning", "Git sync changed a post in place while its send was still out, and that send "
                                            "succeeded: the platform has the post as it was sent, the post says so, and "
                                            "the next sync sends the change as an update, or reports the file stale."),
    "auth.login.refused": ("warning", "A sign-in was refused by the allow-list, role or password check."),
    "cache.backend.unavailable": ("warning", "Redis/Valkey did not answer; rate limits are per replica until it is back."),
    "outbox.consumer.failed": ("warning", "An event sink failed on a batch; the cursor stayed and the batch is retried."),
    "outbox.consumer.retired": ("warning", "Housekeeping removed the cursor of a consumer the code no longer has, idle "
                                           "for RETIRE_IDLE (its name only): the events only it had not passed are "
                                           "purged by retention."),
    "outbox.consumer.registered": ("info", "Housekeeping gave a consumer the code has, but that has not run yet, its "
                                           "cursor at the start (its name only): the purge waits for it."),
    "identity.user.disabled": ("warning", "Keycloak disabled or deleted a person: their Inkcast tokens and PATs were cut off."),
    "identity.user.held": ("warning", "Keycloak reports a person disabled while the sync's breaker holds a pass: their OAuth "
                                      "tokens were cut, their PATs are refused but not revoked, until an operator decides."),
    "identity.user.released": ("info", "The hold on a person was lifted (Keycloak reports them enabled again, or the pass was "
                                       "dismissed): their PATs work again."),
    "identity.erasure.orphans_swept": ("warning", "Housekeeping removed rows written for people in the instant of their "
                                                  "erasure, before the write-time guard refused such writes (rows and tables, "
                                                  "counts only); once per table, not a new deletion, no audit row."),
    "identity.user.deleted": ("warning", "A person's account is gone (deleted by them, or in Keycloak and past the grace "
                                         "period): every row of theirs was erased (ids and counts only)."),
    "identity.erasure.reapplied": ("warning", "A restore brought back rows of a person erased before; they were erased again at "
                                              "start-up, before anything served (id and count only; not a new deletion)."),
    "identity.erasure.wal_pending": ("warning", "An erasure committed, but the checkpoint after it could not cut the "
                                                "write-ahead log (a reader on an older snapshot, or it failed), so the "
                                                "erased rows' bytes stay in the -wal file, and their pages in the database "
                                                "file, until housekeeping's checkpoint within 30 minutes (frame count "
                                                "only). A volume snapshot taken meanwhile copies them (#404)."),
    "identity.erasure_hash.backfill": ("info", "The one-off fill of users.erasure_hash at start-up, after the upgrade to it or "
                                               "a restore of a backup from before it, one line per batch: rows filled so far, "
                                               "of the rows without a hash at this start, and batches. A start cut short "
                                               "keeps the batches it committed; the next start fills the rest."),
    "identity.erasure_hash.backfilled": ("info", "That fill is done: every user has a hash (rows filled and batches at this "
                                                 "start)."),
    "identity.erasure_hash.filled_with_records": ("warning", "That fill ran beside existing erasure records (rows filled, "
                                                             "records, people re-erased at this start): the rows carry the "
                                                             "current key's hash, so a person among them erased before a "
                                                             "rotation of INKCAST_SECRET_KEY is matched by no record (#112); "
                                                             "the Keycloak sync's first pass covers them. Counts only."),
    "identity.sync.failed": ("warning", "A Keycloak user sync pass failed (service account refused, Keycloak down); retried."),
    "identity.sync.person.failed": ("warning", "Keycloak answered an error about one person, or gave the read about them no "
                                               "answer (Inkcast user id, read, status and the error's name only); nothing "
                                               "was done for them, the pass went on, they are read again."),
    "identity.sync.pass.ended": ("warning", "A sync pass ended early, a success for what it did: its budget of reads about "
                                            "people with no answer was used (the stage and the count only); what it did "
                                            "not reach waits for the next pass or reconcile."),
    "identity.sync.breaker.tripped": ("critical", "A reconcile, or the admin-events passes of one reconcile interval, found "
                                                  "too many people gone from Keycloak or disabled in it at once (pass id, "
                                                  "which pass and counts); nobody is cut off or erased, the disabled keep "
                                                  "their PATs refused, until an operator confirms or dismisses the pass "
                                                  "with inkcast identity."),
    "identity.sync.breaker.held": ("warning", "The breaker is still open (pass id, how many people are held, gone and "
                                              "disabled); every pass says so until the pass is resolved."),
    "identity.sync.breaker.confirmed": ("warning", "An operator confirmed a pass (audit: pass id, who, how many applied by "
                                                   "kind, marked, cut off elsewhere and skipped): its people are cut off; "
                                                   "the gone start their grace "
                                                   "period, the disabled lose their PATs."),
    "identity.sync.breaker.dismissed": ("warning", "An operator dismissed a pass (audit: pass id, who, how many released): "
                                                   "nothing was done to the gone, the disabled's PATs work again; the next "
                                                   "reconcile reads everyone again."),
    "identity.sync.breaker.refused": ("warning", "An operator's confirm or dismiss did nothing: no such pass, or it was "
                                                 "resolved already (pass id only)."),
    "authz.denied": ("warning", "A permission check said no (user, relation and object ids; never a token or a name)."),
    "authz.error": ("critical", "The policy engine could not answer (OpenFGA down, bad model id); the caller failed closed (503)."),
    "authz.model.pinned": ("info", "Start-up resolved the OpenFGA store by INKCAST_FGA_STORE_NAME and pinned the model version "
                                   "equal to the packaged model (store and model ids); a refusal before it is config.unsafe."),
    "backup.done": ("info", "A database backup was taken, checked and kept (name, size, rows), and the backups and pre-restore "
                            "copies INKCAST_BACKUP_KEEP or more days old removed (removed_backups, removed_copies)."),
    "backup.failed": ("critical", "A database backup failed; nothing was removed. The alert fires when none succeeds for 26 h."),
    "backup.retention.refused": ("critical", "The backup was taken and is good (its backup.done line precedes this), but "
                                             "nothing aged out: the newest backup from before today has aged out "
                                             "(INKCAST_BACKUP_KEEP days old or more), which is the node's clock that far "
                                             "ahead (check it) or no backup for that long (InkcastBackupMissing has fired "
                                             "as long). The command exits 1 so the Job fails and that alert fires; on a "
                                             "correct clock the next good backup removes them (#405)."),
    "restore.done": ("warning", "The database was replaced by a backup; the previous one was kept beside it."),
    "restore.erasures.carried": ("info", "The erasure records of the replaced database, and their aliases from a key "
                                         "rotation, went into the restored copy, so the server erases those people again "
                                         "at start-up (counts only)."),
    "restore.refused": ("warning", "A restore did not run: the backup failed its checks, or the server was still running."),
    "restore.staging.removed": ("warning", "The backup removed the staging copy a restore killed outright had left beside "
                                           "the database (.inkcast.db.restoring, with its journal; bytes): a whole copy of "
                                           "a backup, whoever it holds included, that no restore was running (none held the "
                                           "data directory's lock) and nothing reads (#404)."),
    "restore.stopped": ("warning", "A restore stopped before its swap, asked to (SIGTERM, Ctrl-C) or past its deadline; the "
                                   "current database was not touched and nothing was left staged."),
}

# ---- the public route templates ---------------------------------------------------------------
# Every public route, as its template. Two things derive from this list: the route label of the access metrics
# (``route_label`` below: a Prometheus label must come from a closed set, and a path is not one) and the redaction of
# the one-time tickets that travel in a path. tests/test_libs.py checks that every path in the OpenAPI document maps
# to itself; tests/test_credential_path_redaction.py walks every mounted route and fails on a credential-bearing
# parameter no template here covers.

ROUTE_TEMPLATES: tuple[str, ...] = (
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
    "/api/v1/posts", "/api/v1/posts/{post_id}/cancel", "/api/v1/posts/{post_id}/resend", "/api/v1/preview",
    "/api/v1/stories:sync", "/api/v1/ledger",
    "/api/v1/insights/summary", "/api/v1/insights/kpis",
    "/livez", "/readyz", "/healthz", "/metrics",
)

# Path parameters that carry a credential. A one-time ticket is a bearer capability (whoever holds the link acts as
# the person, issue #149), so each template above that has one becomes a redaction pattern: the ticket is masked
# wherever the path is logged, in the access log, a rejected request or a traceback, in both log formats, and a new
# one-time route is masked the day its template is listed. An id ({post_id}, {token_id}) is not a credential and stays.
CREDENTIAL_PATH_PARAMS: frozenset[str] = frozenset({"ticket"})
_TICKET = r"[A-Za-z0-9_\-]{8,}"                  # what a ticket looks like (secrets.token_urlsafe); never a short id
_SEGMENT = r"[^/\s\"']+"                         # a plain path segment, inside a log line: ends at a slash, space or quote
_PARAM = re.compile(r"(\{[a-z_]+\})")


def credential_path_patterns(templates: Iterable[str]) -> list[re.Pattern[str]]:
    """One pattern per credential-bearing parameter of the templates: the path up to it is kept (group 1) and the
    credential replaced, so ``/account/delete/{ticket}`` gives ``(/account/delete/)[A-Za-z0-9_\\-]{8,}``. A plain
    parameter before it matches one segment."""
    patterns = []
    for template in templates:
        parts = _PARAM.split(template)
        for i, part in enumerate(parts):
            if part.startswith("{") and part[1:-1] in CREDENTIAL_PATH_PARAMS:
                prefix = "".join(_SEGMENT if p.startswith("{") else re.escape(p) for p in parts[:i])
                patterns.append(re.compile("(" + prefix + ")" + _TICKET))
    return patterns


# Bearer secrets Inkcast mints or handles, and the usual ways secrets appear in text.
_SECRET_PATTERNS = [
    re.compile(r"\b(ink_pat_[A-Za-z0-9]+_)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(ic_(?:at|rt|code)_)[A-Za-z0-9_\-]{8,}"),
    *credential_path_patterns(ROUTE_TEMPLATES),   # one-time tickets travel in the path: /connect/…, /account/delete/…
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
    # Never log request URLs with query strings at INFO (they can carry OAuth codes), nor, at DEBUG, httpcore2's copy
    # of every response's headers (Set-Cookie values: the consent and deletion cookies through the Gateway).
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# ---- route labels for RED metrics ----------------------------------------------------------
# The access metrics carry the *route template* (``ROUTE_TEMPLATES`` above), never the raw path (ids, tickets,
# typos); a path that matches none is labelled "other".

_ROUTE_TEMPLATES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile("^" + _PARAM.sub("[^/]+", template) + "$"), template) for template in ROUTE_TEMPLATES
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
