# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Runtime configuration, read from environment variables (prefixed INKCAST_, OIDC_ or GITHUB_).

Every service reads the same ``Config``; ``validate(needs)`` checks only what the services in this
process need, so a SchedulerWorker doesn't demand sign-in secrets and a Gateway doesn't demand
a database. The launcher refuses to start on a problem and exits 78 (EX_CONFIG).

Settings are plain values. The four credentials (``secret_key``, ``oidc_client_secret``,
``github_client_secret``, ``password``) are ``SecretValue``s that come through the ``SecretsProvider``
(env, ``NAME_FILE``, a mounted directory, or a secrets manager; see libs/secrets): call ``.reveal()`` at
the point of use. A ``Config(...)`` built in tests with plain strings gets the same wrapping.

``INKCAST_CONFIG_FILE`` (``.env``, ``.yaml`` or ``.json``) may supply the same names as the environment;
real environment variables win, and the file may hold secret *names* (``INKCAST_SECRET_KEY_FILE``), never
secret values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping
from urllib.parse import urlparse

from .secrets import (
    REDACTOR, ConfigFileError, NamingError, SecretFetchError, SecretsProvider, SecretValue, fetcher_from_environ,
    load_config_file,
)

ENVIRONMENTS = ("development", "ci", "staging", "production")
# Which credential each service "need" covers: a Gateway (no needs) may run with INKCAST_PASSWORD_FILE set
# and nothing mounted; an IdentityService ("login") must be able to read it.
SECRET_SETTINGS: dict[str, str] = {
    "INKCAST_SECRET_KEY": "secret_key",
    "OIDC_CLIENT_SECRET": "login",
    "GITHUB_CLIENT_SECRET": "login",
    "INKCAST_PASSWORD": "login",
}


class ConfigError(RuntimeError):
    pass


class _Env:
    """Typed reads of one settings mapping; every conversion problem becomes a ConfigError, not a traceback."""

    def __init__(self, environ: Mapping[str, str]):
        self.environ = environ

    def get(self, name: str, default: str = "") -> str:
        """Read NAME, or the file named by NAME_FILE (how Kubernetes/OpenShift Secrets are best mounted)."""
        path = self.environ.get(f"{name}_FILE", "").strip()
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read().strip() or default
            except OSError as exc:
                raise ConfigError(f"{name}_FILE points to {path!r}, which can't be read: {exc}") from exc
        return self.environ.get(name, "").strip() or default

    def list(self, name: str) -> tuple[str, ...]:
        return tuple(v.strip().lower() for v in self.environ.get(name, "").split(",") if v.strip())

    def bool(self, name: str) -> bool:
        return self.get(name).lower() in {"1", "true", "yes", "on"}

    def int(self, name: str, default: str) -> int:
        raw = self.get(name, default)
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a whole number (got {raw!r}).") from None

    def float(self, name: str, default: str) -> float:
        raw = self.get(name, default)
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a number (got {raw!r}).") from None

    def prefixed(self, prefix: str, key_fn) -> dict[str, str]:
        return {key_fn(key[len(prefix):]): value.strip().rstrip("/")
                for key, value in self.environ.items() if key.startswith(prefix) and value.strip()}


@dataclass(frozen=True)
class Config:
    public_url: str
    secret_key: SecretValue  # a plain str is accepted and wrapped
    database_path: str
    host: str = "127.0.0.1"
    port: int = 8787
    # development | ci | staging | production (INKCAST_ENV). Production tightens the posture checks; the
    # container image defaults to production, a laptop to development.
    environment: str = "development"

    # How people sign in:
    #   "oidc"     any OpenID Connect provider, e.g. a Keycloak realm (which can broker Google, GitHub, ...)
    #   "github"   GitHub OAuth app directly (small teams without an identity provider)
    #   "password" one shared password (single person, local use)
    login: str = "oidc"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretValue = SecretValue("")
    oidc_scopes: str = "openid email profile"
    oidc_allowed_email_domains: tuple[str, ...] = ()
    oidc_required_role: str = ""  # e.g. "inkcast-user"; read from oidc_role_claim
    oidc_role_claim: str = "realm_access.roles"  # dotted path in userinfo (Keycloak default)
    allow_any_user: bool = False
    github_client_id: str = ""
    github_client_secret: SecretValue = SecretValue("")
    github_url: str = "https://github.com"
    github_api_url: str = "https://api.github.com"
    github_allowed_users: tuple[str, ...] = ()
    github_allowed_orgs: tuple[str, ...] = ()
    password: SecretValue = SecretValue("")  # for login="password"

    access_token_seconds: int = 3600
    refresh_token_seconds: int = 60 * 60 * 24 * 30
    scheduler_interval_seconds: float = 20.0
    log_format: str = "text"  # "json" in containers
    log_level: str = "INFO"
    metrics_port: int = 9090  # Prometheus /metrics on its own port; 0 disables
    shutdown_grace_seconds: float = 25.0
    extra_allowed_hosts: tuple[str, ...] = ()
    # Per-platform API base URL overrides, e.g. {"devto": "https://community.example.com/api"}
    platform_urls: dict[str, str] = field(default_factory=dict)

    # Which service(s) this process runs (CamelCase, comma-separated), and where they serve:
    # "/" (default), "auto" (= /<full-name>, e.g. /publishing-api), or an explicit path.
    app_name: str = "All"
    path_prefix: str = "/"
    # Gateway only: base URL of each upstream service by full name, from INKCAST_UPSTREAM_<NAME>,
    # e.g. INKCAST_UPSTREAM_PUBLISHING_API=http://publishing-api:8080/publishing-api
    upstreams: dict[str, str] = field(default_factory=dict)
    # Scheduled posts this long past due mean the scheduler is behind or stopped (alert + readiness).
    overdue_grace_seconds: float = 600.0
    # Lab troubleshooting only (INKCAST_LOG_REVEAL_SECRETS): DEBUG lines about consumed secrets also print
    # the real value. Refused in production.
    reveal_secrets: bool = False
    # The provider the four credentials came through (None for a Config built by hand): status() for the
    # metrics port, refresh() after an auth failure, on_change() for rotation.
    secrets: SecretsProvider | None = field(default=None, repr=False, compare=False)
    # A credential that could not be read at load time (a dangling NAME_FILE, an unreachable secrets manager),
    # by setting name. Deferred to validate(): it only matters to a service that needs that credential.
    secret_errors: dict[str, str] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        for name in ("secret_key", "oidc_client_secret", "github_client_secret", "password"):
            value = getattr(self, name)
            if not isinstance(value, SecretValue):
                value = SecretValue(value)
                object.__setattr__(self, name, value)
            REDACTOR.register(value.reveal())  # masked everywhere, however this Config was built

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def mcp_url(self) -> str:
        return self.public_url.rstrip("/") + "/mcp"

    @property
    def allowed_hosts(self) -> list[str]:
        netloc = urlparse(self.public_url).netloc
        hosts = {netloc, f"localhost:{self.port}", f"127.0.0.1:{self.port}", *self.extra_allowed_hosts}
        return sorted(h for h in hosts if h)

    def validate(self, needs: Iterable[str] = ("login", "secret_key")) -> None:
        """Raise ConfigError listing every problem. ``needs``: "login" (IdentityService), "secret_key"
        (anything touching tokens or credentials), "upstreams" (Gateway)."""
        needs = set(needs)
        problems = []
        if self.environment not in ENVIRONMENTS:
            problems.append(f"INKCAST_ENV must be one of {', '.join(ENVIRONMENTS)} (got {self.environment!r}).")
        if self.is_production and self.reveal_secrets:
            problems.append("INKCAST_LOG_REVEAL_SECRETS must not be enabled in production: it writes secret values to the log.")
        if self.path_prefix != "auto" and not self.path_prefix.startswith("/"):
            problems.append("INKCAST_PATH_PREFIX must be '/', 'auto' or a path starting with '/'.")
        for name, need in SECRET_SETTINGS.items():
            if need in needs and name in self.secret_errors:
                problems.append(self.secret_errors[name])
        if "upstreams" in needs:
            if not self.upstreams:
                problems.append("The Gateway needs INKCAST_UPSTREAM_<SERVICE> URLs, e.g. "
                                "INKCAST_UPSTREAM_PUBLISHING_API=http://publishing-api:8080.")
            for name, url in sorted(self.upstreams.items()):
                if urlparse(url).scheme not in ("http", "https"):
                    problems.append(f"INKCAST_UPSTREAM_{name.upper().replace('-', '_')} must be an http(s) URL.")
        if "secret_key" in needs and len(self.secret_key.reveal()) < 32:
            problems.append(
                "INKCAST_SECRET_KEY must be at least 32 characters; generate one with "
                "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
        if "login" not in needs:
            pass
        elif self.login == "oidc":
            if not (self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret):
                problems.append("OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET are required for OIDC sign-in.")
            if not (self.oidc_allowed_email_domains or self.oidc_required_role or self.allow_any_user):
                problems.append(
                    "Restrict who can sign in with OIDC_REQUIRED_ROLE and/or OIDC_ALLOWED_EMAIL_DOMAINS "
                    "(or INKCAST_ALLOW_ANY_USER=true for open sign-up, e.g. a SaaS realm)."
                )
        elif self.login == "github":
            if not (self.github_client_id and self.github_client_secret):
                problems.append("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET are required for GitHub sign-in.")
            if not (self.github_allowed_users or self.github_allowed_orgs or self.allow_any_user):
                problems.append(
                    "Set GITHUB_ALLOWED_USERS and/or GITHUB_ALLOWED_ORGS so only your people can sign in "
                    "(or INKCAST_ALLOW_ANY_USER=true if you really mean anyone)."
                )
        elif self.login == "password":
            if len(self.password.reveal()) < 12:
                problems.append("INKCAST_PASSWORD must be at least 12 characters for password sign-in.")
        else:
            problems.append("INKCAST_LOGIN must be 'oidc', 'github' or 'password'.")
        if urlparse(self.public_url).scheme != "https" and urlparse(self.public_url).hostname not in {
            "localhost", "127.0.0.1"
        }:
            problems.append("INKCAST_PUBLIC_URL must use https unless it is localhost.")
        if problems:
            raise ConfigError("Inkcast configuration problems:\n- " + "\n- ".join(problems))


def settings_from(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The effective settings: ``INKCAST_CONFIG_FILE`` (names only) under the real environment."""
    real = dict(environ if environ is not None else os.environ)
    path = real.get("INKCAST_CONFIG_FILE", "").strip()
    if not path:
        return real
    try:
        return {**load_config_file(path), **real}
    except ConfigFileError as exc:
        raise ConfigError(str(exc)) from exc


def secrets_provider(settings: Mapping[str, str], service: str = "inkcast") -> SecretsProvider:
    """The process's ``SecretsProvider``, built from INKCAST_SECRETS_SOURCE/_DIR/_TTL and the naming settings."""
    env = _Env(settings)
    try:
        fetcher = fetcher_from_environ(settings, service=service)
    except (SecretFetchError, NamingError) as exc:
        raise ConfigError(str(exc)) from exc
    return SecretsProvider(fetcher, ttl=env.float("INKCAST_SECRETS_TTL", "300"),
                           max_stale=env.float("INKCAST_SECRETS_MAX_STALE", "3600"),
                           reveal_in_logs=env.bool("INKCAST_LOG_REVEAL_SECRETS"))


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Build the ``Config`` from the environment (plus ``INKCAST_CONFIG_FILE``). Raises ``ConfigError`` for anything
    that can't be read or parsed; ``validate()`` then checks the values."""
    settings = settings_from(environ)
    env = _Env(settings)
    provider = secrets_provider(settings, service=env.get("INKCAST_APP_NAME", "All"))

    secret_errors: dict[str, str] = {}

    def secret(name: str) -> SecretValue:
        # Deferred, not fatal: a Gateway pod gets the shared ConfigMap's INKCAST_PASSWORD_FILE too, but mounts
        # no secrets. validate(needs) reports the problem for the services that need this credential.
        try:
            return provider.get(name)
        except (OSError, SecretFetchError, NamingError) as exc:
            secret_errors[name] = f"{name} can't be read: {exc}"
            return SecretValue("")

    port = env.int("PORT", env.get("INKCAST_PORT", "8787"))
    return Config(
        public_url=env.get("INKCAST_PUBLIC_URL", f"http://localhost:{port}").rstrip("/"),
        secret_key=secret("INKCAST_SECRET_KEY"),
        database_path=env.get("INKCAST_DATABASE", "./inkcast.db"),
        host=env.get("HOST", env.get("INKCAST_HOST", "127.0.0.1")),
        port=port,
        environment=env.get("INKCAST_ENV", "development").lower(),
        login=env.get("INKCAST_LOGIN", "oidc").lower(),
        oidc_issuer=env.get("OIDC_ISSUER").rstrip("/"),
        oidc_client_id=env.get("OIDC_CLIENT_ID"),
        oidc_client_secret=secret("OIDC_CLIENT_SECRET"),
        oidc_scopes=env.get("OIDC_SCOPES", "openid email profile"),
        oidc_allowed_email_domains=env.list("OIDC_ALLOWED_EMAIL_DOMAINS"),
        oidc_required_role=env.get("OIDC_REQUIRED_ROLE"),
        oidc_role_claim=env.get("OIDC_ROLE_CLAIM", "realm_access.roles"),
        allow_any_user=env.bool("INKCAST_ALLOW_ANY_USER"),
        github_client_id=env.get("GITHUB_CLIENT_ID"),
        github_client_secret=secret("GITHUB_CLIENT_SECRET"),
        github_url=env.get("GITHUB_URL", "https://github.com").rstrip("/"),
        github_api_url=env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        github_allowed_users=env.list("GITHUB_ALLOWED_USERS"),
        github_allowed_orgs=env.list("GITHUB_ALLOWED_ORGS"),
        password=secret("INKCAST_PASSWORD"),
        extra_allowed_hosts=tuple(h.strip() for h in settings.get("INKCAST_ALLOWED_HOSTS", "").split(",") if h.strip()),
        platform_urls=env.prefixed("INKCAST_API_URL_", str.lower),
        app_name=env.get("INKCAST_APP_NAME", "All"),
        path_prefix=env.get("INKCAST_PATH_PREFIX", "/"),
        upstreams=env.prefixed("INKCAST_UPSTREAM_", lambda k: k.lower().replace("_", "-")),
        overdue_grace_seconds=env.float("INKCAST_OVERDUE_GRACE", "600"),
        scheduler_interval_seconds=env.float("INKCAST_SCHEDULER_INTERVAL", "20"),
        log_format=env.get("INKCAST_LOG_FORMAT", "text").lower(),
        log_level=env.get("INKCAST_LOG_LEVEL", "INFO"),
        metrics_port=env.int("INKCAST_METRICS_PORT", "9090"),
        shutdown_grace_seconds=env.float("INKCAST_SHUTDOWN_GRACE", "25"),
        reveal_secrets=env.bool("INKCAST_LOG_REVEAL_SECRETS"),
        secrets=provider,
        secret_errors=secret_errors,
    )
