# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Runtime security posture checks, run at startup and served (metadata only) on the metrics port.

Each check returns ``pass | warn | fail | info | skip`` with a plain-language explanation. The launcher
treats ``fail`` as a configuration error (exit 78, like ``Config.validate``) and logs each ``warn`` once
with the event ``config.posture.warn``. ``inkcast checks`` prints the table.

Add a check by writing a ``_check_<name>(ctx) -> Check`` function and listing it in ``CHECKS``. A check
must never raise, never read a secret value it does not need, and never put a value in ``detail``.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import urlparse

from .redactor import REDACTOR, Redactor, is_sensitive_key

if TYPE_CHECKING:  # pragma: no cover - avoids a config ↔ secrets import cycle
    from ..config import Config
    from .provider import SecretsProvider

Status = str  # "pass" | "warn" | "fail" | "info" | "skip"

_SWITCH_VALUE = re.compile(r"^(true|false|yes|no|on|off|0|1)$", re.I)
_PLACEHOLDER_KEYS = ("changeme", "change-me", "secret", "password", "example", "default", "inkcast", "test", "1234")
_LOCALHOST = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    status: Status
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail}


@dataclass
class _Ctx:
    config: "Config"
    provider: "SecretsProvider | None"
    redactor: Redactor
    environ: Mapping[str, str]

    @property
    def production(self) -> bool:
        return getattr(self.config, "environment", "development") == "production"

    def level(self, soft: Status = "warn") -> Status:
        """``fail`` in production, ``soft`` elsewhere: the same finding, a stricter verdict where it matters."""
        return "fail" if self.production else soft


def _world_readable(path: str) -> bool | None:
    try:
        return bool(os.stat(path).st_mode & 0o004)
    except OSError:
        return None


# ---- the checks --------------------------------------------------------------------------


def _check_public_url(ctx: _Ctx) -> Check:
    parsed = urlparse(ctx.config.public_url)
    if parsed.scheme == "https":
        return Check("public-url-https", "Public URL uses https", "pass", f"{ctx.config.public_url}")
    if parsed.hostname in _LOCALHOST:
        return Check("public-url-https", "Public URL uses https", "info",
                     f"{ctx.config.public_url} is plain http on localhost; fine for a laptop, never for a server.")
    return Check("public-url-https", "Public URL uses https", "fail",
                 f"{ctx.config.public_url} is plain http: tokens and passwords would cross the network in the clear. "
                 "Set INKCAST_PUBLIC_URL to the https address people use.")


def _check_secret_key(ctx: _Ctx) -> Check:
    key = ctx.config.secret_key.reveal()
    if not key:  # a Gateway runs without one; Config.validate reports it where it is needed
        return Check("secret-key-strength", "INKCAST_SECRET_KEY is strong", "skip", "No INKCAST_SECRET_KEY in this process.")
    if len(key) < 32:
        return Check("secret-key-strength", "INKCAST_SECRET_KEY is strong", "fail",
                     f"{len(key)} characters; at least 32 are required. Generate one with "
                     "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`.")
    lowered = key.lower()
    if any(word in lowered for word in _PLACEHOLDER_KEYS):
        return Check("secret-key-strength", "INKCAST_SECRET_KEY is strong", ctx.level(),
                     "The key contains a placeholder word (changeme, example, test...). Generate a random one.")
    if len(set(key)) < 16:
        return Check("secret-key-strength", "INKCAST_SECRET_KEY is strong", ctx.level(),
                     f"Only {len(set(key))} distinct characters: this is not a random key. Generate one with "
                     "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`.")
    return Check("secret-key-strength", "INKCAST_SECRET_KEY is strong", "pass", f"{len(key)} characters, random-looking.")


def _check_reveal_flag(ctx: _Ctx) -> Check:
    if getattr(ctx.config, "reveal_secrets", False):
        return Check("no-reveal-flag", "Secret values kept out of logs", ctx.level(),
                     "INKCAST_LOG_REVEAL_SECRETS=true: DEBUG lines print real credential values. Lab use only; "
                     "refused in production.")
    return Check("no-reveal-flag", "Secret values kept out of logs", "pass",
                 f"Logs (level {ctx.config.log_level}) show secret names, lengths and fingerprints only.")


def _check_debug_flags(ctx: _Ctx) -> Check:
    flags = [name for name in ("PYTHONINSPECT", "PYTHONDEVMODE", "PYTHONDEBUG", "PYTHONTRACEMALLOC")
             if ctx.environ.get(name)]
    if ctx.config.log_level.upper() == "DEBUG":
        flags.append("INKCAST_LOG_LEVEL=DEBUG")
    if flags and ctx.production:
        return Check("debug-flags", "No debug flags in production", "warn",
                     f"{', '.join(flags)} set in production: more detail in logs and slower code paths. Unset them.")
    if flags:
        return Check("debug-flags", "No debug flags in production", "info", f"{', '.join(flags)} set (not production).")
    return Check("debug-flags", "No debug flags in production", "pass", "No debug flags.")


def _check_log_format(ctx: _Ctx) -> Check:
    if ctx.production and ctx.config.log_format != "json":
        return Check("log-format-json", "JSON logs in production", "warn",
                     f"INKCAST_LOG_FORMAT={ctx.config.log_format}: log stores and alert rules read JSON. Set it to json.")
    return Check("log-format-json", "JSON logs in production", "pass", f"INKCAST_LOG_FORMAT={ctx.config.log_format}.")


def _secret_files(ctx: _Ctx) -> list[str]:
    names = ("INKCAST_SECRET_KEY", "OIDC_CLIENT_SECRET", "GITHUB_CLIENT_SECRET", "INKCAST_PASSWORD", "INKCAST_CACHE_PASSWORD")
    paths: list[str] = []
    for name in names:
        path = ctx.environ.get(f"{name}_FILE", "").strip()
        if path and path not in paths:
            paths.append(path)
        if ctx.provider is not None:
            for p in ctx.provider.paths(name):
                if p not in paths:
                    paths.append(p)
    return paths


def _check_secret_files(ctx: _Ctx) -> Check:
    paths = _secret_files(ctx)
    if not paths:
        return Check("secret-files-protected", "Secret files not world-readable", "skip", "No secret files in use.")
    exposed = [p for p in paths if _world_readable(p)]
    if exposed:
        return Check("secret-files-protected", "Secret files not world-readable", "warn",
                     f"World-readable: {', '.join(exposed)}. Mount the Secret with defaultMode 0440 "
                     "(deploy/kustomize does) or chmod 0400 the file.")
    return Check("secret-files-protected", "Secret files not world-readable", "pass",
                 f"{len(paths)} file(s), readable by owner/group only.")


def _check_env_scrubbed(ctx: _Ctx) -> Check:
    """Credentials in environment variables leak through /proc/<pid>/environ, child processes, crash reports
    and `kubectl describe pod`. The NAME_FILE and INKCAST_SECRETS_DIR paths avoid that."""
    flagged = []
    for name, value in ctx.environ.items():
        if not value or name.startswith("INKCAST_SECRET_ID_"):
            continue
        looks_like = is_sensitive_key(name) and not _SWITCH_VALUE.match(value)
        if ctx.redactor.contains(value) or looks_like:
            flagged.append(name)
    if flagged:
        return Check("env-scrubbed", "No credentials in environment variables", "warn",
                     f"{', '.join(sorted(flagged))} hold credential values. Use {flagged[0]}_FILE or INKCAST_SECRETS_DIR "
                     "(a mounted Secret) instead; env vars leak through /proc, child processes and crash reports.")
    return Check("env-scrubbed", "No credentials in environment variables", "pass",
                 "No loaded secret value or credential-like variable found in the environment.")


def _check_redaction(ctx: _Ctx) -> Check:
    n = ctx.redactor.size
    if n == 0:
        return Check("redaction-active", "Log and error redaction active", "warn",
                     "No secret values registered yet; only pattern-based redaction applies.")
    return Check("redaction-active", "Log and error redaction active", "pass",
                 f"{n} secret value encodings registered; every log line and traceback is scrubbed.")


def _check_non_root(ctx: _Ctx) -> Check:
    uid = getattr(os, "getuid", lambda: -1)()
    if uid == 0:
        return Check("non-root", "Process is not root", "warn", "Running as root. Use the image's USER 1001 or a service user.")
    if uid < 0:
        return Check("non-root", "Process is not root", "skip", "Not a POSIX platform.")
    return Check("non-root", "Process is not root", "pass", f"Running as uid {uid}.")


def _check_core_dumps(ctx: _Ctx) -> Check:
    try:
        with open("/proc/self/limits", encoding="utf-8") as f:
            line = next((ln for ln in f if ln.startswith("Max core file size")), "")
    except OSError:
        return Check("core-dumps", "Core dumps disabled", "skip", "Not on Linux.")
    soft = re.split(r"\s{2,}", line.strip())[1] if line else "?"
    if soft == "0":
        return Check("core-dumps", "Core dumps disabled", "pass", "Core file size limit is 0.")
    advice = ("Kubernetes has no per-pod setting: set it in the container runtime's default ulimits."
              if ctx.environ.get("KUBERNETES_SERVICE_HOST") else "Set LimitCORE=0 (systemd) or --ulimit core=0 (Docker).")
    return Check("core-dumps", "Core dumps disabled", "warn",
                 f"Core file limit is {soft!r}: a crash dump would contain decrypted credentials. {advice}")


def _check_forwarded_ips(ctx: _Ctx) -> Check:
    allowed = ctx.environ.get("INKCAST_FORWARDED_ALLOW_IPS", "127.0.0.1")
    if allowed.strip() == "*":
        return Check("forwarded-ips", "Proxy headers trusted from known proxies only", "info",
                     "INKCAST_FORWARDED_ALLOW_IPS=* trusts X-Forwarded-* from any peer. Fine behind a NetworkPolicy "
                     "that only admits the ingress; on an open network name the proxy IPs instead.")
    return Check("forwarded-ips", "Proxy headers trusted from known proxies only", "pass", f"Trusted: {allowed}.")


def _check_login(ctx: _Ctx) -> Check:
    if ctx.production and ctx.config.login == "password":
        return Check("login-method", "Sign-in suits the environment", "warn",
                     "INKCAST_LOGIN=password (one shared password) in production. Prefer oidc (Keycloak) or github.")
    if ctx.production and ctx.config.allow_any_user:
        return Check("login-method", "Sign-in suits the environment", "info",
                     "INKCAST_ALLOW_ANY_USER=true: anyone with an account at the identity provider may sign in (SaaS mode).")
    return Check("login-method", "Sign-in suits the environment", "pass", f"INKCAST_LOGIN={ctx.config.login}.")


def _check_database_file(ctx: _Ctx) -> Check:
    path = ctx.config.database_path
    if path == ":memory:" or not os.path.exists(path):
        return Check("database-protected", "Database file not world-readable", "skip", "No database file yet.")
    if _world_readable(path):
        return Check("database-protected", "Database file not world-readable", "warn",
                     f"{path} is world-readable; it holds token hashes and sealed credentials. chmod 0600 it.")
    return Check("database-protected", "Database file not world-readable", "pass", f"{path} is private.")


def _check_secrets_source(ctx: _Ctx) -> Check:
    if ctx.provider is None:
        return Check("secrets-source", "Secret source", "skip", "No SecretsProvider (Config built by hand).")
    mode = ctx.provider.mode
    absent = [s["name"] for s in ctx.provider.status() if not s["present"]]
    return Check("secrets-source", "Secret source", "info",
                 f"Mode {mode}; TTL {int(ctx.provider.ttl)}s; {len(ctx.provider.names())} secret(s) asked for"
                 + (f", not set: {', '.join(absent)}" if absent else "") + ".")


CHECKS: tuple[Callable[[_Ctx], Check], ...] = (
    _check_public_url, _check_secret_key, _check_reveal_flag, _check_debug_flags, _check_log_format,
    _check_secret_files, _check_env_scrubbed, _check_redaction, _check_non_root, _check_core_dumps,
    _check_forwarded_ips, _check_login, _check_database_file, _check_secrets_source,
)


def run_checks(config: "Config", provider: "SecretsProvider | None" = None, redactor: Redactor | None = None,
               environ: Mapping[str, str] | None = None) -> list[Check]:
    """Every posture check, in order. A check that crashes becomes a ``skip`` with the error's type."""
    ctx = _Ctx(config, provider if provider is not None else getattr(config, "secrets", None),
               redactor if redactor is not None else REDACTOR, environ if environ is not None else os.environ)
    out: list[Check] = []
    for check in CHECKS:
        try:
            out.append(check(ctx))
        except Exception as exc:  # a broken check must not stop the process
            out.append(Check(check.__name__.removeprefix("_check_").replace("_", "-"), check.__name__, "skip",
                             f"check crashed: {type(exc).__name__}"))
    return out


def problems(checks: list[Check]) -> tuple[list[Check], list[Check]]:
    """(hard failures, warnings)."""
    return [c for c in checks if c.status == "fail"], [c for c in checks if c.status == "warn"]


def render(checks: list[Check], out: Any = None) -> None:
    """A table for ``inkcast checks``."""
    out = out or sys.stdout
    for c in checks:
        print(f"{c.status.upper():<5} {c.id:<24} {c.title}\n      {c.detail}", file=out)


__all__ = ["CHECKS", "Check", "problems", "render", "run_checks"]
