# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Runtime configuration, read from environment variables (prefixed INKCAST_, OIDC_ or GITHUB_).

Every service reads the same ``Config``; ``validate(needs)`` checks only what the services in this
process need, so a SchedulerWorker doesn't demand sign-in secrets and a Gateway doesn't demand
a database. The launcher refuses to start on a problem and exits 78 (EX_CONFIG).

Settings are plain values. The credentials (``secret_key``, ``oidc_client_secret``, ``github_client_secret``,
``password``, ``cache_password``, ``keycloak_sync_secret``, ``fga_api_token``) are ``SecretValue``s that come
through the ``SecretsProvider`` (env, ``NAME_FILE``, a mounted directory, or a secrets manager; see libs/secrets):
call ``.reveal()`` at the point of use. A ``Config(...)`` built in tests with plain strings gets the same wrapping.

``INKCAST_CONFIG_FILE`` (``.env``, ``.yaml`` or ``.json``) may supply the same names as the environment;
real environment variables win, and the file may hold secret *names* (``INKCAST_SECRET_KEY_FILE``), never
secret values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
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
    "INKCAST_CACHE_PASSWORD": "secret_key",  # read by every product service (rate limits, locks); not by a Gateway
    "INKCAST_KEYCLOAK_SYNC_SECRET": "login",  # IdentityService's Keycloak service account (E9.F9.5)
    "INKCAST_FGA_API_TOKEN": "authz",  # OpenFGA's preshared key, when INKCAST_AUTHZ_ENGINE=openfga (E3.F3.6)
}
AUTHZ_ENGINES = ("local", "openfga")
DAYS_OR_OFF_RULE = "{name} must be a whole number of days, 0 or more, or 'off' (got {got!r})."
# The publish retry policy (F1.10.S1, issue #89): sends in all, and the seconds between them. The 30 s floor is
# there because a 429 means "slow down"; the ceiling (6 h) and the count keep the whole window a few days at most.
PUBLISH_MAX_ATTEMPTS = (1, 10)
PUBLISH_RETRY_DELAYS = (1, 9)       # how many delays may be listed
PUBLISH_RETRY_DELAY = (30, 21600)   # seconds, each
PUBLISH_ATTEMPTS_RULE = "INKCAST_PUBLISH_MAX_ATTEMPTS must be a whole number from 1 to 10 (got {got!r})."
PUBLISH_DELAYS_RULE = ("INKCAST_PUBLISH_RETRY_DELAYS must be 1 to 9 whole numbers of seconds, comma-separated, each from 30 "
                       "to 21600 and never decreasing (got {got!r}).")


class ConfigError(RuntimeError):
    pass


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class RetryPolicy:
    """How the scheduler retries a post whose send certainly never happened (a connection failure, a 429):
    ``max_attempts`` sends in all, ``delays`` seconds between them, the last delay repeating. Built from
    INKCAST_PUBLISH_MAX_ATTEMPTS and INKCAST_PUBLISH_RETRY_DELAYS and handed to the Publisher (libs/pipeline), which
    applies it only to ``retryable`` platform errors: an ``uncertain`` one (the post may have gone out) is never retried,
    whatever the policy."""

    max_attempts: int = 4
    delays: tuple[int, ...] = (60, 300, 1800)

    def delay_after(self, attempt: int) -> int:
        """Seconds to wait after the ``attempt``-th send (counted from 1) before the next; the last delay repeats."""
        return self.delays[min(attempt, len(self.delays)) - 1]

    @property
    def window_seconds(self) -> int:
        """From the first send to the last: how long a post that keeps failing is retried before it is marked failed."""
        return sum(self.delay_after(attempt) for attempt in range(1, self.max_attempts))


def publish_retry_problems(policy: RetryPolicy, got: tuple[Any, Any] | None = None) -> list[str]:
    """The rules of INKCAST_PUBLISH_MAX_ATTEMPTS and INKCAST_PUBLISH_RETRY_DELAYS, as problems naming the variable;
    ``got`` is what to quote (the raw strings at load, the values for a Config built by hand)."""
    got_attempts, got_delays = got if got else (policy.max_attempts, policy.delays)
    problems = []
    attempts, delays = policy.max_attempts, policy.delays
    if not (_whole(attempts) and PUBLISH_MAX_ATTEMPTS[0] <= attempts <= PUBLISH_MAX_ATTEMPTS[1]):
        problems.append(PUBLISH_ATTEMPTS_RULE.format(got=got_attempts))
    if not (isinstance(delays, tuple) and PUBLISH_RETRY_DELAYS[0] <= len(delays) <= PUBLISH_RETRY_DELAYS[1]
            and all(_whole(d) and PUBLISH_RETRY_DELAY[0] <= d <= PUBLISH_RETRY_DELAY[1] for d in delays)
            and all(earlier <= later for earlier, later in zip(delays, delays[1:]))):
        problems.append(PUBLISH_DELAYS_RULE.format(got=got_delays))
    return problems


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

    def days_or_off(self, name: str, default: str) -> int | None:
        """A whole number of days (0 or more), or ``off`` for None; anything else is a ConfigError naming the variable."""
        raw = self.get(name, default)
        if raw.lower() == "off":
            return None
        try:
            days = int(raw)
        except ValueError:
            days = -1
        if days < 0:
            raise ConfigError(DAYS_OR_OFF_RULE.format(name=name, got=raw))
        return days

    def publish_retry(self) -> RetryPolicy:
        """INKCAST_PUBLISH_MAX_ATTEMPTS and INKCAST_PUBLISH_RETRY_DELAYS as a ``RetryPolicy``; a value outside its rule
        is a ConfigError naming that variable (both, when both are wrong)."""
        raw_attempts = self.get("INKCAST_PUBLISH_MAX_ATTEMPTS", "4")
        raw_delays = self.get("INKCAST_PUBLISH_RETRY_DELAYS", "60,300,1800")
        try:
            attempts: Any = int(raw_attempts)
        except ValueError:
            attempts = raw_attempts                   # fails the whole-number rule below, with the same message
        try:
            delays: Any = tuple(int(part) for part in raw_delays.split(","))
        except ValueError:
            delays = raw_delays
        policy = RetryPolicy(attempts, delays)
        problems = publish_retry_problems(policy, got=(raw_attempts, raw_delays))
        if len(problems) == 1:
            raise ConfigError(problems[0])
        if problems:
            raise ConfigError("Inkcast configuration problems:\n- " + "\n- ".join(problems))
        return policy

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
    # Keycloak user sync (E9.F9.5): a service account (client credentials, realm-management view-users and
    # view-events) that IdentityService uses to notice disabled or deleted people and cut off their tokens.
    # Empty client id = off. The realm's client is inkcast-sync (deploy/keycloak/realm-inkcast.json).
    keycloak_sync_client_id: str = ""
    keycloak_sync_secret: SecretValue = SecretValue("")
    keycloak_sync_interval_seconds: float = 30.0   # how often admin events are read (the "within a minute" goal)
    keycloak_reconcile_seconds: float = 600.0      # how often every Keycloak-backed user is re-checked (missed events)
    # Days a person Keycloak no longer has (an administrator's deletion, or a self-deletion whose event was never
    # seen) stays cut off before the reconcile erases their data (E9.F9.5.S6, issue #86): 0 erases at the first
    # reconcile that finds them gone; None (INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=off) never erases on a 404, which is a
    # posture warning in production and staging. The grace period lets an accidental deletion be restored.
    keycloak_erase_after_days: int | None = 30

    access_token_seconds: int = 3600
    refresh_token_seconds: int = 60 * 60 * 24 * 30
    scheduler_interval_seconds: float = 20.0
    # How a scheduled post whose send certainly never happened is retried (F1.10.S1, issue #89):
    # INKCAST_PUBLISH_MAX_ATTEMPTS sends in all, INKCAST_PUBLISH_RETRY_DELAYS seconds apart (the last repeats).
    # The default is 4 sends over about 36 minutes. A window over 24 h is a posture warning (publish-retry-window).
    publish_retry: RetryPolicy = RetryPolicy()
    insights_interval_seconds: float = 10.0  # how often InsightsService folds new domain events into KPI rollups
    log_format: str = "text"  # "json" in containers
    log_level: str = "INFO"
    metrics_port: int = 9090  # Prometheus /metrics on its own port; 0 disables
    shutdown_grace_seconds: float = 25.0
    max_body_bytes: int = 10 * 1024 * 1024  # a request body above this is refused with 413 before any handler runs
    # Writes per minute per client address on the endpoints anyone can call without a token (client
    # registration, the token page, the password form, the connect page). 0 disables the limit.
    rate_limit_per_minute: int = 60
    # Shared cache and coordination (libs/cache): empty = in-process; redis://host:6379/0 or rediss:// for
    # Redis/Valkey shared by every replica. The password never goes in the URL: INKCAST_CACHE_PASSWORD(_FILE).
    cache_url: str = ""
    cache_password: SecretValue = SecretValue("")
    # Authorization (E3.F3.6, docs/authorization.md §7-8): "local" evaluates authz/model.json in-process over
    # tuples in the Inkcast database (tests, laptops, small self-hosts); "openfga" asks an OpenFGA server.
    # Production refuses "local" unless INKCAST_AUTHZ_ALLOW_LOCAL says this is a self-host that accepts it.
    authz_engine: str = "local"
    authz_allow_local: bool = False
    authz_model_path: str = ""  # INKCAST_AUTHZ_MODEL: a model.json to use instead of the packaged one (local engine)
    fga_api_url: str = ""
    fga_store_id: str = ""
    fga_model_id: str = ""  # every deploy pins a model version; a change is a new version, never an edit
    fga_api_token: SecretValue = SecretValue("")  # preshared-key bearer auth; empty when OpenFGA runs without one
    fga_timeout_seconds: float = 5.0
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
    # How many daily backups the backup CronJob keeps (its INKCAST_BACKUP_KEEP; libs/db/backup.DEFAULT_KEEP): the
    # deletion pages tell a person that copies of their data in backups age out within this many days (issue #94).
    # The server reads the same name, so a changed CronJob value goes in the shared ConfigMap too.
    backup_keep: int = 14
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
        for name in ("secret_key", "oidc_client_secret", "github_client_secret", "password", "cache_password",
                     "keycloak_sync_secret", "fga_api_token"):
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

    def validate(self, needs: Iterable[str] = ("login", "secret_key", "authz")) -> None:
        """Raise ConfigError listing every problem. ``needs``: "login" (IdentityService), "secret_key"
        (anything touching tokens or credentials), "authz" (anything that checks permissions), "upstreams"
        (Gateway)."""
        needs = set(needs)
        problems = []
        if self.environment not in ENVIRONMENTS:
            problems.append(f"INKCAST_ENV must be one of {', '.join(ENVIRONMENTS)} (got {self.environment!r}).")
        if self.is_production and self.reveal_secrets:
            problems.append("INKCAST_LOG_REVEAL_SECRETS must not be enabled in production: it writes secret values to the log.")
        if self.path_prefix != "auto" and not self.path_prefix.startswith("/"):
            problems.append("INKCAST_PATH_PREFIX must be '/', 'auto' or a path starting with '/'.")
        if self.backup_keep < 1:
            problems.append("INKCAST_BACKUP_KEEP must be at least 1 (the backup keeps that many daily copies).")
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
        if self.cache_url:
            from .cache import parse_cache_url
            try:
                parse_cache_url(self.cache_url)
            except ValueError as exc:
                problems.append(str(exc))
        if "authz" in needs:
            problems += self._authz_problems()
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
        if "login" in needs and self.keycloak_sync_client_id:
            if self.login != "oidc":
                problems.append("INKCAST_KEYCLOAK_SYNC_CLIENT_ID needs INKCAST_LOGIN=oidc with a Keycloak OIDC_ISSUER.")
            if not self.keycloak_sync_secret:
                problems.append("INKCAST_KEYCLOAK_SYNC_SECRET is required with INKCAST_KEYCLOAK_SYNC_CLIENT_ID.")
            if "/realms/" not in self.oidc_issuer:
                problems.append("Keycloak sync needs a Keycloak OIDC_ISSUER (https://<host>/realms/<realm>).")
        days = self.keycloak_erase_after_days          # None is "off"; load_config parses, this covers a Config built by hand
        if days is not None and not (isinstance(days, int) and days >= 0):
            problems.append(DAYS_OR_OFF_RULE.format(name="INKCAST_KEYCLOAK_ERASE_AFTER_DAYS", got=days))
        problems += publish_retry_problems(self.publish_retry)  # load_config refuses already; a Config built by hand
        if urlparse(self.public_url).scheme != "https" and urlparse(self.public_url).hostname not in {
            "localhost", "127.0.0.1"
        }:
            problems.append("INKCAST_PUBLIC_URL must use https unless it is localhost.")
        if problems:
            raise ConfigError("Inkcast configuration problems:\n- " + "\n- ".join(problems))

    def _authz_problems(self) -> list[str]:
        """E3.F3.6.S4. The hosted SaaS runs OpenFGA (ADR-0007); nothing in the configuration says "hosted", so
        the smallest honest rule is: production and staging refuse the local engine unless the operator states,
        with INKCAST_AUTHZ_ALLOW_LOCAL=true, that this is a self-host that accepts it. The hosted environments set
        that flag to false explicitly."""
        problems = []
        if self.authz_engine not in AUTHZ_ENGINES:
            return [f"INKCAST_AUTHZ_ENGINE must be one of {', '.join(AUTHZ_ENGINES)} (got {self.authz_engine!r})."]
        if self.authz_engine == "openfga":
            if not (self.fga_api_url and self.fga_store_id and self.fga_model_id):
                problems.append("INKCAST_AUTHZ_ENGINE=openfga needs INKCAST_FGA_API_URL, INKCAST_FGA_STORE_ID and "
                                "INKCAST_FGA_MODEL_ID (the pinned model version).")
            if self.fga_api_url and urlparse(self.fga_api_url).scheme not in ("http", "https"):
                problems.append("INKCAST_FGA_API_URL must be an http(s) URL.")
            if not 0 < self.fga_timeout_seconds < float("inf"):  # also nan, which httpx2 reads as "no timeout"
                problems.append("INKCAST_FGA_TIMEOUT must be a positive number of seconds.")
        elif self.environment in ("production", "staging") and not self.authz_allow_local:
            # Staging too (owner, 2026-10-22): it rehearses production, so it runs what production runs.
            problems.append(f"INKCAST_AUTHZ_ENGINE=local is refused in {self.environment}: the hosted service runs OpenFGA "
                            "(INKCAST_AUTHZ_ENGINE=openfga). A self-host that accepts the in-process engine sets "
                            "INKCAST_AUTHZ_ALLOW_LOCAL=true.")
        return problems


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
    # Never in production, not even for the lines written while the configuration loads (before validate() refuses).
    reveal = env.bool("INKCAST_LOG_REVEAL_SECRETS") and env.get("INKCAST_ENV", "development").lower() != "production"
    return SecretsProvider(fetcher, ttl=env.float("INKCAST_SECRETS_TTL", "300"),
                           max_stale=env.float("INKCAST_SECRETS_MAX_STALE", "3600"), reveal_in_logs=reveal)


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
    cache_url = env.get("INKCAST_CACHE_URL").rstrip("/")
    authz_engine = env.get("INKCAST_AUTHZ_ENGINE", "local").lower()
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
        keycloak_sync_client_id=env.get("INKCAST_KEYCLOAK_SYNC_CLIENT_ID"),
        # Asked for only when the sync is configured: no credential is fetched that nothing uses.
        keycloak_sync_secret=(secret("INKCAST_KEYCLOAK_SYNC_SECRET") if env.get("INKCAST_KEYCLOAK_SYNC_CLIENT_ID")
                              else SecretValue("")),
        keycloak_sync_interval_seconds=env.float("INKCAST_KEYCLOAK_SYNC_INTERVAL", "30"),
        keycloak_reconcile_seconds=env.float("INKCAST_KEYCLOAK_RECONCILE_INTERVAL", "600"),
        keycloak_erase_after_days=env.days_or_off("INKCAST_KEYCLOAK_ERASE_AFTER_DAYS", "30"),
        extra_allowed_hosts=tuple(h.strip() for h in settings.get("INKCAST_ALLOWED_HOSTS", "").split(",") if h.strip()),
        platform_urls=env.prefixed("INKCAST_API_URL_", str.lower),
        app_name=env.get("INKCAST_APP_NAME", "All"),
        path_prefix=env.get("INKCAST_PATH_PREFIX", "/"),
        upstreams=env.prefixed("INKCAST_UPSTREAM_", lambda k: k.lower().replace("_", "-")),
        overdue_grace_seconds=env.float("INKCAST_OVERDUE_GRACE", "600"),
        backup_keep=env.int("INKCAST_BACKUP_KEEP", "14"),
        scheduler_interval_seconds=env.float("INKCAST_SCHEDULER_INTERVAL", "20"),
        publish_retry=env.publish_retry(),
        insights_interval_seconds=env.float("INKCAST_INSIGHTS_INTERVAL", "10"),
        log_format=env.get("INKCAST_LOG_FORMAT", "text").lower(),
        log_level=env.get("INKCAST_LOG_LEVEL", "INFO"),
        metrics_port=env.int("INKCAST_METRICS_PORT", "9090"),
        shutdown_grace_seconds=env.float("INKCAST_SHUTDOWN_GRACE", "25"),
        max_body_bytes=env.int("INKCAST_MAX_BODY_BYTES", str(10 * 1024 * 1024)),
        rate_limit_per_minute=env.int("INKCAST_RATE_LIMIT_PER_MINUTE", "60"),
        cache_url=cache_url,
        # Asked for only when a shared cache is configured: no credential is fetched that nothing uses.
        cache_password=secret("INKCAST_CACHE_PASSWORD") if cache_url else SecretValue(""),
        authz_engine=authz_engine,
        authz_allow_local=env.bool("INKCAST_AUTHZ_ALLOW_LOCAL"),
        authz_model_path=env.get("INKCAST_AUTHZ_MODEL"),
        fga_api_url=env.get("INKCAST_FGA_API_URL").rstrip("/"),
        fga_store_id=env.get("INKCAST_FGA_STORE_ID"),
        fga_model_id=env.get("INKCAST_FGA_MODEL_ID"),
        # Asked for only when OpenFGA is the engine: no credential is fetched that nothing uses.
        fga_api_token=secret("INKCAST_FGA_API_TOKEN") if authz_engine == "openfga" else SecretValue(""),
        fga_timeout_seconds=env.float("INKCAST_FGA_TIMEOUT", "5"),
        reveal_secrets=env.bool("INKCAST_LOG_REVEAL_SECRETS"),
        secrets=provider,
        secret_errors=secret_errors,
    )
