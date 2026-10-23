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
    names = ("INKCAST_SECRET_KEY", "OIDC_CLIENT_SECRET", "GITHUB_CLIENT_SECRET", "INKCAST_PASSWORD", "INKCAST_CACHE_PASSWORD",
             "INKCAST_KEYCLOAK_SYNC_SECRET", "INKCAST_FGA_API_TOKEN")  # every key of config.SECRET_SETTINGS
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


def _sync_in_use(ctx: _Ctx) -> bool:
    """Whether the Keycloak user sync runs at all: sign-in through a Keycloak realm, and the sync's service account set."""
    config = ctx.config
    return (getattr(config, "login", "") == "oidc" and "/realms/" in getattr(config, "oidc_issuer", "")
            and bool(getattr(config, "keycloak_sync_client_id", "")))


def _hosted(ctx: _Ctx) -> bool:
    return getattr(ctx.config, "environment", "development") in ("production", "staging")


# Beyond this many days (ten years) the erasure grace period is a way of never erasing, and is reported as one.
EFFECTIVELY_NEVER_DAYS = 3650


def _check_erase_after(ctx: _Ctx) -> Check:
    """E9.F9.5.S6 (issue #86): deletion promises erasure. With INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=off a person Keycloak
    no longer has (an administrator's deletion, a self-deletion whose event was never seen) is cut off but their data
    stays until an operator erases it: that is a choice production and staging must see; so is a number of days no
    one will live to see (#105). A number is only ``pass`` where the sync runs: elsewhere it is noted, not promised."""
    days = getattr(ctx.config, "keycloak_erase_after_days", 30)
    title = "People gone from Keycloak are erased"
    if days is None:
        return Check("keycloak-erase-after", title, "warn" if _hosted(ctx) else "info",
                     "INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=off: a person Keycloak no longer has is cut off, but their posts, "
                     "ledger and credentials stay until an operator erases them. Set a number of days (the default is 30).")
    if days > EFFECTIVELY_NEVER_DAYS:
        return Check("keycloak-erase-after", title, "warn" if _hosted(ctx) else "info",
                     f"INKCAST_KEYCLOAK_ERASE_AFTER_DAYS={days}: more than ten years is effectively off. A person Keycloak no "
                     "longer has is cut off, but their data stays. Set a number of days (the default is 30).")
    when = "at the first reconcile that finds them gone" if days == 0 else f"{days} day(s) after the cut-off"
    if not _sync_in_use(ctx):
        return Check("keycloak-erase-after", title, "info",
                     f"INKCAST_KEYCLOAK_ERASE_AFTER_DAYS={days}: not in use. Sign-in is not a Keycloak realm, or the user sync "
                     "is off (INKCAST_KEYCLOAK_SYNC_CLIENT_ID unset), so nobody is found gone and nobody is erased by it.")
    return Check("keycloak-erase-after", title, "pass", f"INKCAST_KEYCLOAK_ERASE_AFTER_DAYS={days}: erased {when}.")


def _check_breaker(ctx: _Ctx) -> Check:
    """E9.F9.5.S7 (issue #105) and S8 (#135): a Keycloak that answers 404, or ``enabled: false``, about many people who
    still exist (restored from an older backup, the wrong issuer, a user-storage provider switched off) must not cut
    everyone off at once. ``off`` is a choice production and staging must see."""
    title = "A mass deletion or disabling in Keycloak waits for an operator"
    config = ctx.config
    if not getattr(config, "keycloak_breaker", True):
        return Check("keycloak-breaker", title, "warn" if _hosted(ctx) else "info",
                     "INKCAST_KEYCLOAK_BREAKER=off: a reconcile that finds many people gone from Keycloak, or disabled in it, "
                     "at once cuts them all off at once (their personal access tokens revoked for good) and erases the gone "
                     "after the grace period, however wrong Keycloak's answer is (a realm restored from an older backup, the "
                     "wrong issuer, a user-storage provider switched off). Set it to on.")
    minimum, percent = getattr(config, "keycloak_breaker_min", 5), getattr(config, "keycloak_breaker_percent", 10)
    if percent >= 100:                      # it trips above the percent: more than everyone read is never gone
        return Check("keycloak-breaker", title, "warn" if _hosted(ctx) else "info",
                     f"INKCAST_KEYCLOAK_BREAKER_PERCENT={percent}: more than all of the people read can never be gone, so "
                     "the breaker never trips: effectively off. Set it lower (the default is 10).")
    if not _sync_in_use(ctx):
        return Check("keycloak-breaker", title, "info",
                     "INKCAST_KEYCLOAK_BREAKER=on: not in use. Sign-in is not a Keycloak realm, or the user sync is off "
                     "(INKCAST_KEYCLOAK_SYNC_CLIENT_ID unset).")
    return Check("keycloak-breaker", title, "pass",
                 f"INKCAST_KEYCLOAK_BREAKER=on: a reconcile that finds at least {minimum} people, and more than {percent}% of "
                 "those it read, gone from Keycloak or disabled in it holds them all for an operator (inkcast identity "
                 "confirm-deletions): the disabled keep their personal access tokens, refused, meanwhile.")


def _duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{n} {unit}" for n, unit in ((hours, "h"), (minutes, "min"), (secs, "s")) if n]
    return " ".join(parts) or "0 s"


def _check_publish_retry_window(ctx: _Ctx) -> Check:
    """F1.10.S1 (issue #89): a scheduled post whose send certainly never happened is retried INKCAST_PUBLISH_MAX_ATTEMPTS
    times in all, INKCAST_PUBLISH_RETRY_DELAYS apart. The settings allow a window of over two days; a post still being
    retried a day after its time is one its author should hear about instead."""
    policy = getattr(ctx.config, "publish_retry", None)
    title = "Publish retries end within a day"
    if policy is None:
        return Check("publish-retry-window", title, "skip", "No retry policy on this Config.")
    settings = (f"INKCAST_PUBLISH_MAX_ATTEMPTS={policy.max_attempts}, "
                f"INKCAST_PUBLISH_RETRY_DELAYS={','.join(str(d) for d in policy.delays)}")
    window, longest = policy.window_seconds, policy.longest_window_seconds
    if window > 24 * 3600:
        return Check("publish-retry-window", title, "warn",
                     f"{settings}: a post whose send keeps failing is retried for {_duration(window)} before it is marked "
                     "failed, and could go out that long after its time with nobody told. Fewer attempts or shorter delays "
                     "keep the window within a day.")
    stretched = (f", or after {_duration(longest)} when a 429's Retry-After stretches every wait to the largest delay"
                 if longest > window else "")
    if longest > 24 * 3600:
        return Check("publish-retry-window", title, "warn",
                     f"{settings}: a post whose send keeps failing is marked failed after {_duration(window)}{stretched}, "
                     "and could go out that long after its time with nobody told. Fewer attempts or a smaller largest "
                     "delay keep the window within a day.")
    if policy.max_attempts == 1:
        return Check("publish-retry-window", title, "pass", f"{settings}: one send, no retry.")
    return Check("publish-retry-window", title, "pass",
                 f"{settings}: a post whose send keeps failing is marked failed after {_duration(window)}{stretched} "
                 f"({policy.max_attempts} sends).")


def _check_pat_expiry(ctx: _Ctx) -> Check:
    """F1.10.S2 (issue #90): a personal access token lasts INKCAST_PAT_DEFAULT_DAYS unless asked otherwise, at most
    INKCAST_PAT_MAX_DAYS, and never only while INKCAST_PAT_ALLOW_NO_EXPIRY. Allowing one that never expires is a
    choice production and staging should see on the table, not a problem: ``info`` there. Tokens already issued keep
    their expiry whatever the settings say now."""
    policy = getattr(ctx.config, "pat", None)
    title = "Personal access tokens expire"
    if policy is None:
        return Check("pat-expiry", title, "skip", "No token policy on this Config.")
    settings = (f"INKCAST_PAT_DEFAULT_DAYS={policy.default_days}, INKCAST_PAT_MAX_DAYS={policy.max_days}, "
                f"INKCAST_PAT_ALLOW_NO_EXPIRY={'true' if policy.allow_no_expiry else 'false'}")
    if policy.allow_no_expiry:
        hosted = getattr(ctx.config, "environment", "development") in ("production", "staging")
        return Check("pat-expiry", title, "info" if hosted else "pass",
                     f"{settings}: a person may create a token that never expires (the API's null, the page's never), "
                     "to revoke when no longer needed. Set INKCAST_PAT_ALLOW_NO_EXPIRY=false to require an expiry; "
                     "tokens already issued keep theirs.")
    return Check("pat-expiry", title, "pass",
                 f"{settings}: every new token expires, within {policy.max_days} days ({policy.default_days} unless asked).")


# F1.10.S5 (issue #93): the sync interval at a minute or more breaks the "cut off within a minute" promise (README,
# E9.F9.5.S2, the deletion page); the reconcile's bound is hard (libs/config.KEYCLOAK_RECONCILE_CEILING), not a warning.
SYNC_INTERVAL_WARN_SECONDS = 60
# Daily copies: above this a deleted person's data lingers in backups over five weeks (issue #94).
BACKUP_KEEP_WARN = 35
# Writes a minute from one client address: above this (ten a second, which no person submitting a form reaches) the
# limit no longer slows a tool guessing the one shared password of INKCAST_LOGIN=password (#157). Still far under the
# ceiling (libs/config.RATE_LIMIT_CEILING), which is about the limiter's arithmetic, not the posture.
RATE_LIMIT_WARN = 600


def _check_sync_interval(ctx: _Ctx) -> Check:
    """A person disabled or deleted in Keycloak loses their tokens within a minute: the events pass runs every
    INKCAST_KEYCLOAK_SYNC_INTERVAL seconds, so at 60 or more the promise is broken whether or not the sync runs today."""
    seconds = getattr(ctx.config, "keycloak_sync_interval_seconds", 30.0)
    title = "People gone from Keycloak are cut off within a minute"
    shown = format(seconds, "g")
    if seconds >= SYNC_INTERVAL_WARN_SECONDS:
        return Check("keycloak-sync-interval", title, "warn",
                     f"INKCAST_KEYCLOAK_SYNC_INTERVAL={shown}: Keycloak's events are read every {shown} s, so a person "
                     "disabled or deleted there keeps their Inkcast tokens for longer than the minute the deletion page and "
                     "the README promise. Set it below 60 (the default is 30).")
    if not _sync_in_use(ctx):
        return Check("keycloak-sync-interval", title, "info",
                     f"INKCAST_KEYCLOAK_SYNC_INTERVAL={shown}: not in use. Sign-in is not a Keycloak realm, or the user sync "
                     "is off (INKCAST_KEYCLOAK_SYNC_CLIENT_ID unset).")
    return Check("keycloak-sync-interval", title, "pass",
                 f"INKCAST_KEYCLOAK_SYNC_INTERVAL={shown}: Keycloak's events are read every {shown} s.")


def _check_rate_limit(ctx: _Ctx) -> Check:
    """F1.10.S5 (issue #93): INKCAST_RATE_LIMIT_PER_MINUTE=0 turns off the per-client limit on the endpoints anyone can
    call without a token. With INKCAST_LOGIN=password that is unbounded guessing of the one shared password: a failure
    in production, a warning elsewhere; a limit above RATE_LIMIT_WARN with password sign-in is effectively the same
    (#157): a warning. With another sign-in it is a choice on record (``info``)."""
    per_minute = getattr(ctx.config, "rate_limit_per_minute", 60)
    title = "Unauthenticated writes are rate-limited"
    password = getattr(ctx.config, "login", "") == "password"
    if per_minute == 0 and password:
        return Check("rate-limit", title, ctx.level(),
                     "INKCAST_RATE_LIMIT_PER_MINUTE=0 with INKCAST_LOGIN=password: the password form takes unlimited guesses "
                     "from one client. Set a limit (the default is 60 per minute per client address).")
    if per_minute > RATE_LIMIT_WARN and password:
        return Check("rate-limit", title, "warn",
                     f"INKCAST_RATE_LIMIT_PER_MINUTE={per_minute} with INKCAST_LOGIN=password: the password form takes "
                     f"{per_minute} guesses a minute from one client address, more than {RATE_LIMIT_WARN} (ten a second), "
                     "which no longer slows a guessing tool. Set it lower (the default is 60).")
    if per_minute == 0:
        return Check("rate-limit", title, "info",
                     "INKCAST_RATE_LIMIT_PER_MINUTE=0: client registration, the token page and the connect page take unlimited "
                     "writes from one client address (sign-in is at the identity provider). Set a limit to bound them.")
    return Check("rate-limit", title, "pass",
                 f"INKCAST_RATE_LIMIT_PER_MINUTE={per_minute}: writes per minute per client address on the endpoints anyone "
                 "can call without a token.")


def _check_backup_keep(ctx: _Ctx) -> Check:
    """F1.10.S5 (issue #93): a person who deletes their account is erased from the database at once, but copies in
    backups age out only after INKCAST_BACKUP_KEEP days, which the deletion pages state (issue #94). Beyond five weeks
    that is longer than a deletion should linger."""
    keep = getattr(ctx.config, "backup_keep", 14)
    title = "Backups age out within weeks"
    warning = backup_keep_warning(keep)
    if warning:
        return Check("backup-keep", title, "warn", warning)
    return Check("backup-keep", title, "pass",
                 f"INKCAST_BACKUP_KEEP={keep}: a deleted person's data ages out of backups within {keep} days.")


def backup_keep_warning(keep: int, setting: str = "INKCAST_BACKUP_KEEP") -> str | None:
    """The backup-keep warning for ``keep`` daily copies, or None within BACKUP_KEEP_WARN: the check's detail, and what
    ``inkcast backup`` logs when ``--keep`` (``setting``) or the variable says more (#157)."""
    if keep <= BACKUP_KEEP_WARN:
        return None
    return (f"{setting}={keep}: a deleted person's data stays in backups for {keep} days, more than the {BACKUP_KEEP_WARN} "
            "the deletion pages should promise. Keep fewer daily copies (the default is 14), the same for the CronJob and "
            "the server.")


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
    _check_forwarded_ips, _check_login, _check_erase_after, _check_breaker, _check_publish_retry_window,
    _check_pat_expiry, _check_sync_interval, _check_rate_limit, _check_backup_keep,
    _check_database_file, _check_secrets_source,
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
