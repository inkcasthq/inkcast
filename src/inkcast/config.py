# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Runtime configuration, read from environment variables (all prefixed INKCAST_ or GITHUB_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    """Read NAME, or the file named by NAME_FILE (how Kubernetes/OpenShift Secrets are best mounted)."""
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip() or default
        except OSError as exc:
            raise ConfigError(f"{name}_FILE points to {path!r}, which can't be read: {exc}") from exc
    return os.environ.get(name, "").strip() or default


def _list(name: str) -> tuple[str, ...]:
    return tuple(v.strip().lower() for v in os.environ.get(name, "").split(",") if v.strip())


def _bool(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    public_url: str
    secret_key: str
    database_path: str
    host: str = "127.0.0.1"
    port: int = 8787

    # How people sign in:
    #   "oidc"     any OpenID Connect provider, e.g. a Keycloak realm (which can broker Google, GitHub, ...)
    #   "github"   GitHub OAuth app directly (small teams without an identity provider)
    #   "password" one shared password (single person, local use)
    login: str = "oidc"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    oidc_allowed_email_domains: tuple[str, ...] = ()
    oidc_required_role: str = ""  # e.g. "inkcast-user"; read from oidc_role_claim
    oidc_role_claim: str = "realm_access.roles"  # dotted path in userinfo (Keycloak default)
    allow_any_user: bool = False
    github_client_id: str = ""
    github_client_secret: str = ""
    github_url: str = "https://github.com"
    github_api_url: str = "https://api.github.com"
    github_allowed_users: tuple[str, ...] = ()
    github_allowed_orgs: tuple[str, ...] = ()
    password: str = ""  # for login="password"

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

    @property
    def mcp_url(self) -> str:
        return self.public_url.rstrip("/") + "/mcp"

    @property
    def allowed_hosts(self) -> list[str]:
        netloc = urlparse(self.public_url).netloc
        hosts = {netloc, f"localhost:{self.port}", f"127.0.0.1:{self.port}", *self.extra_allowed_hosts}
        return sorted(h for h in hosts if h)

    def validate(self) -> None:
        problems = []
        if len(self.secret_key) < 32:
            problems.append(
                "INKCAST_SECRET_KEY must be at least 32 characters; generate one with "
                "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
        if self.login == "oidc":
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
            if len(self.password) < 12:
                problems.append("INKCAST_PASSWORD must be at least 12 characters for password sign-in.")
        else:
            problems.append("INKCAST_LOGIN must be 'oidc', 'github' or 'password'.")
        if urlparse(self.public_url).scheme != "https" and urlparse(self.public_url).hostname not in {
            "localhost", "127.0.0.1"
        }:
            problems.append("INKCAST_PUBLIC_URL must use https unless it is localhost.")
        if problems:
            raise ConfigError("Inkcast configuration problems:\n- " + "\n- ".join(problems))


def load_config() -> Config:
    port = int(_env("PORT", _env("INKCAST_PORT", "8787")))
    platform_urls = {
        key[len("INKCAST_API_URL_"):].lower(): value.strip().rstrip("/")
        for key, value in os.environ.items()
        if key.startswith("INKCAST_API_URL_") and value.strip()
    }
    return Config(
        public_url=_env("INKCAST_PUBLIC_URL", f"http://localhost:{port}").rstrip("/"),
        secret_key=_env("INKCAST_SECRET_KEY"),
        database_path=_env("INKCAST_DATABASE", "./inkcast.db"),
        host=_env("HOST", _env("INKCAST_HOST", "127.0.0.1")),
        port=port,
        login=_env("INKCAST_LOGIN", "oidc").lower(),
        oidc_issuer=_env("OIDC_ISSUER").rstrip("/"),
        oidc_client_id=_env("OIDC_CLIENT_ID"),
        oidc_client_secret=_env("OIDC_CLIENT_SECRET"),
        oidc_scopes=_env("OIDC_SCOPES", "openid email profile"),
        oidc_allowed_email_domains=_list("OIDC_ALLOWED_EMAIL_DOMAINS"),
        oidc_required_role=_env("OIDC_REQUIRED_ROLE"),
        oidc_role_claim=_env("OIDC_ROLE_CLAIM", "realm_access.roles"),
        allow_any_user=_bool("INKCAST_ALLOW_ANY_USER"),
        github_client_id=_env("GITHUB_CLIENT_ID"),
        github_client_secret=_env("GITHUB_CLIENT_SECRET"),
        github_url=_env("GITHUB_URL", "https://github.com").rstrip("/"),
        github_api_url=_env("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        github_allowed_users=_list("GITHUB_ALLOWED_USERS"),
        github_allowed_orgs=_list("GITHUB_ALLOWED_ORGS"),
        password=_env("INKCAST_PASSWORD"),
        extra_allowed_hosts=tuple(h.strip() for h in os.environ.get("INKCAST_ALLOWED_HOSTS", "").split(",") if h.strip()),
        platform_urls=platform_urls,
        scheduler_interval_seconds=float(_env("INKCAST_SCHEDULER_INTERVAL", "20")),
        log_format=_env("INKCAST_LOG_FORMAT", "text").lower(),
        log_level=_env("INKCAST_LOG_LEVEL", "INFO"),
        metrics_port=int(_env("INKCAST_METRICS_PORT", "9090")),
        shutdown_grace_seconds=float(_env("INKCAST_SHUTDOWN_GRACE", "25")),
    )
