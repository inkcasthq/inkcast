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

import math
import os
import re
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
# What creates the OpenFGA store and writes the packaged model (E3.F3.9.S6): the init container of every Inkcast pod
# that checks permissions (deploy/kustomize/components/openfga/authz-model.yaml), named in the refusals that wait for it.
AUTHZ_MODEL_WRITER = "the init container authz-model (`inkcast authz write-model`)"
# OpenFGA's store-name rule (openfga/api openfga_service.proto, CreateStoreRequest.name and ListStoresRequest.name),
# applied to INKCAST_FGA_STORE_NAME at start-up so a name the server would refuse is named here, before any call.
FGA_STORE_NAME = re.compile(r"[a-zA-Z0-9\s.\-/^_&@]{3,64}", re.ASCII)
DAYS_OR_OFF_RULE = "{name} must be a whole number of days, 0 or more, or 'off' (got {got!r})."
# The publish retry policy (F1.10.S1, issue #89): sends in all, and the seconds between them. The 30 s floor is
# there because a 429 means "slow down"; the ceiling (6 h) and the count keep the whole window a few days at most.
PUBLISH_MAX_ATTEMPTS = (1, 10)
PUBLISH_RETRY_DELAYS = (1, 9)       # how many delays may be listed
PUBLISH_RETRY_DELAY = (30, 21600)   # seconds, each
PUBLISH_ATTEMPTS_RULE = "INKCAST_PUBLISH_MAX_ATTEMPTS must be a whole number from 1 to 10 (got {got!r})."
PUBLISH_DELAYS_RULE = ("INKCAST_PUBLISH_RETRY_DELAYS must be 1 to 9 whole numbers of seconds, comma-separated, each from 30 "
                       "to 21600 and never decreasing (got {got!r}).")
# How long a post whose send was interrupted stays 'recovering' (#216): asked about at the platform every stuck pass,
# while the platform refuses the lookup. Past the cap it is 'unconfirmed' for a person: it is never sent late on its own.
# The floor is an hour, the InkcastPostRecoveringTooLong alert's threshold: below it a platform that rate-limits its
# lookups for a few minutes would end posts unconfirmed that it could have resolved. The ceiling is a week: the cap
# bounds how late a post the platform finally reports missing may go out without a person deciding.
PUBLISH_RECOVERY_MAX_HOURS = (1, 168)
PUBLISH_RECOVERY_MAX_RULE = ("INKCAST_PUBLISH_RECOVERY_MAX_HOURS must be a whole number of hours from 1 to 168: how long a post "
                             "whose send was interrupted is checked with the platform before it is unconfirmed for a person, "
                             "never sent late on its own (got {got!r}).")
ON_OFF_RULE = "{name} must be 'on' or 'off' (got {got!r})."
BREAKER_PERCENT_RULE = "INKCAST_KEYCLOAK_BREAKER_PERCENT must be a whole number from 1 to 100 (got {got!r})."
BREAKER_MIN_RULE = "INKCAST_KEYCLOAK_BREAKER_MIN must be a whole number, 1 or more (got {got!r})."
# Token lifetimes (F1.10.S2, issue #90). 366 days is the absolute ceiling on a personal access token: TokenCreate's
# le=366 in the API document, and the most INKCAST_PAT_MAX_DAYS may allow. A refresh token (MCP sign-in) lasts at most
# 90 days: an MCP client left unused that long signs in again.
PAT_DAYS_CEILING = 366
REFRESH_TOKEN_DAYS = (1, 90)
PAT_MAX_RULE = "INKCAST_PAT_MAX_DAYS must be a whole number of days from 1 to 366 (got {got!r})."
PAT_DEFAULT_RULE = "INKCAST_PAT_DEFAULT_DAYS must be a whole number of days from 1 to INKCAST_PAT_MAX_DAYS ({max}) (got {got!r})."
PAT_NO_EXPIRY_RULE = "INKCAST_PAT_ALLOW_NO_EXPIRY must be true or false (got {got!r})."
REFRESH_TOKEN_RULE = "INKCAST_REFRESH_TOKEN_DAYS must be a whole number of days from 1 to 90 (got {got!r})."
# Retention (F1.10.S3, issue #91): domain events in the outbox once every consumer has passed them, and hourly KPI
# buckets with the kpi_applied rows that stop a replayed event counting twice (observability design §6). The hourly
# floor is 31 days because grain=hour serves up to 31 days (services/insights/rest.MAX_HOURLY_DAYS). The hourly
# retention may never be shorter than the outbox's: an event replayed from the outbox is harmless only while its
# kpi_applied row still exists, and that row is purged at the hourly cutoff.
OUTBOX_RETENTION_DAYS = (1, 30)
KPI_HOURLY_RETENTION_DAYS = (31, 400)
OUTBOX_RETENTION_RULE = "INKCAST_OUTBOX_RETENTION_DAYS must be a whole number of days from 1 to 30 (got {got!r})."
KPI_HOURLY_RETENTION_RULE = "INKCAST_KPI_HOURLY_RETENTION_DAYS must be a whole number of days from 31 to 400 (got {got!r})."
RETENTION_ORDER_RULE = ("INKCAST_KPI_HOURLY_RETENTION_DAYS ({hourly}) must not be shorter than INKCAST_OUTBOX_RETENTION_DAYS "
                        "({outbox}): kpi_applied is the record that stops a replayed event counting twice, so it must outlive "
                        "the events.")
# One-time links (F1.10.S4, issue #92): how long a connect link (/connect/<ticket>, where a platform key is typed) and an
# account-deletion link (/account/delete/<ticket>) last, INKCAST_ONE_TIME_LINK_MINUTES, one value for both. 60 minutes
# is a hard maximum, not a tuning knob: a connect link is a bearer capability that passes through the AI transcript
# (the MCP reply hands it to the assistant), and whoever holds it can attach their own platform account to the person's
# Inkcast (services/connectors), so an hour is the most a leaked transcript may be good for. The floor of 5 leaves time
# to switch to a browser and paste a key. The default is the 15 minutes that were hard-coded before.
ONE_TIME_LINK_MINUTES = (5, 60)
ONE_TIME_LINK_DEFAULT_MINUTES = 15
ONE_TIME_LINK_RULE = "INKCAST_ONE_TIME_LINK_MINUTES must be a whole number of minutes from 5 to 60 (got {got!r})."
# The intervals, the overdue grace, the rate limit and the backup copies (F1.10.S5, issue #93; the remaining bounds,
# #157): settings that existed before the newer ones above and took unsafe values silently. Every interval is a finite
# number of seconds, 1 or more (0, a negative, nan or inf is a tight loop, or no wait at all, and 0.0001 is the same
# loop); only the shutdown grace may be a fraction of a second. The Keycloak reconcile may not run more often than the
# events pass (it is the catch-up for events that pass missed), and never so rarely that the InkcastIdentitySyncPersonFailing
# alert (libs/alerting), which looks at a 30-minute window, could miss a person who fails only in the reconcile: the
# reconcile interval, plus the up to one sync interval it waits for the first pass after it is due, plus a margin for
# the reconcile's own run (the next is due that long after the last one *ends*), for Prometheus's scrape (30 s) and for
# its rule evaluation (up to a minute), stays within the window. That is a hard bound, not a posture warning: the alert
# is the operator's only signal that someone is neither cut off nor erased, and a warning logged once at start-up would
# be missed for the lifetime of a deployment whose alert stays blind. The scheduler tick is 1 to 60 s: 120 s or more
# fails the on-time KPI and SLO by design. The overdue grace is at least two ticks and at most an hour: below that, posts
# due between two ticks count as overdue and the critical InkcastPostsOverdue alert fires in normal operation; above it,
# that alert, the operator's only signal that the scheduler is behind or stopped, stays quiet for over an hour (86400
# silences it for a day). The shutdown grace stays under the Deployment's terminationGracePeriodSeconds: at that point
# Kubernetes kills the pod, grace or no grace (scripts/gen-manifests.py and deploy/kustomize/base/deployment.yaml give
# it this number). The rate limit has a ceiling because the limiter keeps the bucket as a float: a value no float can
# hold (310 digits) made every rate-limited request, sign-in included, a 500; 10000 a minute from one client address is
# more than this service serves, so above it the setting is 0 spelled longer, and 0 is how to say off. The sync interval
# at 60 s or more, the rate limit off or above 600 with password sign-in, and backups kept beyond 35 days are posture
# checks (libs/secrets/checks).
KEYCLOAK_RECONCILE_CEILING = 1800      # seconds: the InkcastIdentitySyncPersonFailing alert's window
KEYCLOAK_RECONCILE_MARGIN = 120        # seconds of the window kept free: the reconcile's run, a scrape, an evaluation
SCHEDULER_INTERVAL = (1, 60)           # seconds, inclusive
INTERVAL_FLOOR = 1                     # seconds: the sync and insights intervals (the scheduler's is SCHEDULER_INTERVAL[0])
OVERDUE_GRACE_TICKS = 2                # the overdue grace is at least this many scheduler intervals
OVERDUE_GRACE_CEILING = 3600           # seconds, inclusive: an hour
SHUTDOWN_GRACE_CEILING = 40            # seconds, excluded: the Deployment's terminationGracePeriodSeconds
RATE_LIMIT_CEILING = 10000             # per minute per client address, inclusive
# The reconcile and the sync interval add up to the window less the margin, and the reconcile is no shorter than the
# sync interval, so the sync interval is at most half of that.
SYNC_INTERVAL_CEILING = (KEYCLOAK_RECONCILE_CEILING - KEYCLOAK_RECONCILE_MARGIN) // 2
SYNC_INTERVAL_RULE = ("INKCAST_KEYCLOAK_SYNC_INTERVAL must be a number of seconds from 1 to 840: the reconcile, no shorter "
                      "than it, must follow it within the InkcastIdentitySyncPersonFailing alert's 30-minute window, less a "
                      "120 s margin (got {got!r}).")
RECONCILE_INTERVAL_RULE = ("INKCAST_KEYCLOAK_RECONCILE_INTERVAL must be a number of seconds from INKCAST_KEYCLOAK_SYNC_INTERVAL "
                           "({sync}) to {limit}: the InkcastIdentitySyncPersonFailing alert looks at a 30-minute window "
                           "(1800), the reconcile runs at the first pass after it is due, up to one sync interval late, and "
                           "a 120 s margin covers its own run, Prometheus's scrape and the rule's evaluation; a rarer "
                           "reconcile could never raise the alert (got {got!r}).")
SCHEDULER_INTERVAL_RULE = "INKCAST_SCHEDULER_INTERVAL must be a number of seconds from 1 to 60 (got {got!r})."
OVERDUE_GRACE_RULE = ("INKCAST_OVERDUE_GRACE must be a number of seconds from twice INKCAST_SCHEDULER_INTERVAL ({twice}) to 3600: "
                      "below that, posts due between two ticks count as overdue and the critical InkcastPostsOverdue alert "
                      "fires in normal operation; above an hour, that alert, the only signal that the scheduler is behind "
                      "or stopped, stays quiet that long (got {got!r}).")
SECONDS_FLOOR_RULE = "{name} must be a number of seconds, 1 or more (got {got!r})."
SHUTDOWN_GRACE_RULE = ("INKCAST_SHUTDOWN_GRACE must be a positive number of seconds under 40, the Deployment's "
                       "terminationGracePeriodSeconds: at that point Kubernetes kills the pod, grace or no grace (got {got!r}).")
RATE_LIMIT_RULE = "INKCAST_RATE_LIMIT_PER_MINUTE must be a whole number from 0 (off) to 10000 (got {got!r})."
BACKUP_KEEP_RULE = "INKCAST_BACKUP_KEEP must be a whole number, 1 or more (the backup keeps that many daily copies) (got {got!r})."
_SWITCH = {"true": True, "yes": True, "on": True, "1": True, "false": False, "no": False, "off": False, "0": False}


class ConfigError(RuntimeError):
    pass


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class RetryPolicy:
    """How the scheduler retries a post whose send certainly never happened (a connection failure, a 429):
    ``max_attempts`` sends in all, ``delays`` seconds between them, the last delay repeating; a 429's ``Retry-After``
    lengthens a wait, up to the largest delay. Built from INKCAST_PUBLISH_MAX_ATTEMPTS and INKCAST_PUBLISH_RETRY_DELAYS
    and handed to the Publisher (libs/pipeline), which applies it only to ``retryable`` platform errors: an
    ``uncertain`` one (the post may have gone out) is never retried, whatever the policy."""

    max_attempts: int = 4
    delays: tuple[int, ...] = (60, 300, 1800)

    def delay_after(self, attempt: int, asked: int | float | None = None) -> int:
        """Seconds to wait after the ``attempt``-th send (counted from 1) before the next; the last delay repeats.
        ``asked`` is what the platform's ``Retry-After`` asked for (issue #126): the wait is the longer of the two,
        rounded up to a whole second, but never more than the largest delay listed, so a hostile value can't park a
        post for a week. A value that isn't a finite number is ignored."""
        delay = self.delays[min(attempt, len(self.delays)) - 1]
        try:
            wanted = math.ceil(asked)
        except (TypeError, ValueError, OverflowError):  # None or a string; nan; inf
            return delay
        return min(wanted, max(self.delays)) if wanted > delay else delay

    @property
    def window_seconds(self) -> int:
        """From the first send to the last: how long a post that keeps failing is retried before it is marked failed,
        by the policy's own delays (a 429's ``Retry-After`` can stretch it: ``longest_window_seconds``)."""
        return sum(self.delay_after(attempt) for attempt in range(1, self.max_attempts))

    @property
    def longest_window_seconds(self) -> int:
        """The window when a 429's ``Retry-After`` stretches every wait to the largest delay: the longest a post that
        keeps failing can be retried before it is marked failed."""
        return sum(self.delay_after(attempt, asked=max(self.delays)) for attempt in range(1, self.max_attempts))


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


def publish_recovery_problems(hours: Any, got: Any = None) -> list[str]:
    """The rule of INKCAST_PUBLISH_RECOVERY_MAX_HOURS (a whole number of hours from 1 to 168), as a problem naming it."""
    if _whole(hours) and PUBLISH_RECOVERY_MAX_HOURS[0] <= hours <= PUBLISH_RECOVERY_MAX_HOURS[1]:
        return []
    return [PUBLISH_RECOVERY_MAX_RULE.format(got=hours if got is None else got)]


@dataclass(frozen=True)
class PatPolicy:
    """How long a personal access token may live (F1.10.S2, issue #90): ``default_days`` when the request doesn't say
    (INKCAST_PAT_DEFAULT_DAYS), at most ``max_days`` (INKCAST_PAT_MAX_DAYS), and never only while ``allow_no_expiry``
    (INKCAST_PAT_ALLOW_NO_EXPIRY). Enforced where a token is made (REST, the /tokens/new page, the MCP tool), never on
    tokens already issued: a lower maximum doesn't shorten them."""

    default_days: int = 90
    max_days: int = PAT_DAYS_CEILING
    allow_no_expiry: bool = True

    def refusal(self, days: int | None) -> str | None:
        """Why a token of ``days`` (None = never) can't be made under this policy, naming the limit; None when it can."""
        if days is None:
            if self.allow_no_expiry:
                return None
            return (f"A personal access token must expire on this server (INKCAST_PAT_ALLOW_NO_EXPIRY=false): give it a "
                    f"number of days, at most {self.max_days}.")
        if days > self.max_days:
            return (f"A personal access token may last at most {self.max_days} days on this server "
                    f"(INKCAST_PAT_MAX_DAYS={self.max_days}); {days} is too many.")
        return None


def pat_policy_problems(policy: PatPolicy, got: tuple[Any, Any, Any] | None = None) -> list[str]:
    """The rules of the three INKCAST_PAT_* settings, as problems naming the variable: the maximum from 1 to 366, the
    default from 1 to the maximum, the switch true or false. ``got`` is what to quote (the raw strings at load, the
    values for a Config built by hand)."""
    got_default, got_max, got_allow = got if got else (policy.default_days, policy.max_days, policy.allow_no_expiry)
    problems = []
    max_ok = _whole(policy.max_days) and 1 <= policy.max_days <= PAT_DAYS_CEILING
    if not max_ok:
        problems.append(PAT_MAX_RULE.format(got=got_max))
    ceiling = policy.max_days if max_ok else PAT_DAYS_CEILING
    if not (_whole(policy.default_days) and 1 <= policy.default_days <= ceiling):
        problems.append(PAT_DEFAULT_RULE.format(max=ceiling, got=got_default))
    if not isinstance(policy.allow_no_expiry, bool):
        problems.append(PAT_NO_EXPIRY_RULE.format(got=got_allow))
    return problems


def refresh_token_problems(days: Any, got: Any = None) -> list[str]:
    """The rule of INKCAST_REFRESH_TOKEN_DAYS (a whole number of days from 1 to 90), as a problem naming it."""
    if _whole(days) and REFRESH_TOKEN_DAYS[0] <= days <= REFRESH_TOKEN_DAYS[1]:
        return []
    return [REFRESH_TOKEN_RULE.format(got=days if got is None else got)]


def one_time_link_problems(minutes: Any, got: Any = None) -> list[str]:
    """The rule of INKCAST_ONE_TIME_LINK_MINUTES (a whole number of minutes from 5 to 60), as a problem naming it."""
    if _whole(minutes) and ONE_TIME_LINK_MINUTES[0] <= minutes <= ONE_TIME_LINK_MINUTES[1]:
        return []
    return [ONE_TIME_LINK_RULE.format(got=minutes if got is None else got)]


@dataclass(frozen=True)
class RetentionPolicy:
    """How long the housekeeping keeps what nothing reads any more (F1.10.S3, issue #91): domain events every consumer
    has passed, for ``outbox_days`` (INKCAST_OUTBOX_RETENTION_DAYS, the replay window), and hourly KPI buckets with their
    ``kpi_applied`` rows, for ``kpi_hourly_days`` (INKCAST_KPI_HOURLY_RETENTION_DAYS). Daily buckets are kept. The
    scheduler's housekeeping hands it to ``Store.purge_expired``; nothing reads it from global state."""

    outbox_days: int = 7
    kpi_hourly_days: int = 90

    @property
    def outbox_seconds(self) -> float:
        return self.outbox_days * 86400

    @property
    def kpi_hourly_seconds(self) -> float:
        return self.kpi_hourly_days * 86400


def retention_problems(policy: RetentionPolicy, got: tuple[Any, Any] | None = None) -> list[str]:
    """The rules of INKCAST_OUTBOX_RETENTION_DAYS (1 to 30) and INKCAST_KPI_HOURLY_RETENTION_DAYS (31 to 400, never shorter
    than the outbox's), as problems naming the variable; ``got`` is what to quote (the raw strings at load, the values
    for a Config built by hand)."""
    got_outbox, got_hourly = got if got else (policy.outbox_days, policy.kpi_hourly_days)
    problems = []
    outbox, hourly = policy.outbox_days, policy.kpi_hourly_days
    outbox_ok = _whole(outbox) and OUTBOX_RETENTION_DAYS[0] <= outbox <= OUTBOX_RETENTION_DAYS[1]
    if not outbox_ok:
        problems.append(OUTBOX_RETENTION_RULE.format(got=got_outbox))
    if not (_whole(hourly) and KPI_HOURLY_RETENTION_DAYS[0] <= hourly <= KPI_HOURLY_RETENTION_DAYS[1]):
        problems.append(KPI_HOURLY_RETENTION_RULE.format(got=got_hourly))
    # Judged against an outbox retention within its bounds: one outside them is the problem, not the order. Today the
    # bounds keep the order on their own (31 > 30); the rule is stated anyway, so a change to either bound can't
    # silently lose the invariant.
    if outbox_ok and _whole(hourly) and hourly < outbox:
        problems.append(RETENTION_ORDER_RULE.format(hourly=hourly, outbox=outbox))
    return problems


def _seconds(value: Any) -> bool:
    """A positive, finite number of seconds: an int or a float, never a bool, a nan or an inf."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:                         # an int beyond any float (10**400): no wait, no clock can take it
        return False


def _show(value: Any) -> Any:
    """A number as the operator would write it (30, not 30.0) when quoting a setting inside another's rule."""
    return format(value, "g") if _seconds(value) else value


INTERVAL_SETTINGS = ("INKCAST_KEYCLOAK_SYNC_INTERVAL", "INKCAST_KEYCLOAK_RECONCILE_INTERVAL", "INKCAST_SCHEDULER_INTERVAL",
                     "INKCAST_OVERDUE_GRACE", "INKCAST_INSIGHTS_INTERVAL", "INKCAST_SHUTDOWN_GRACE")


def interval_problems(*, sync: Any, reconcile: Any, scheduler: Any, grace: Any, insights: Any, shutdown: Any,
                      got: Mapping[str, Any] | None = None) -> list[str]:
    """The rules of the six interval settings (F1.10.S5, issue #93; #157), as problems naming the variable: each a finite
    number of seconds, 1 or more (the shutdown grace: positive); the sync interval at most SYNC_INTERVAL_CEILING; the
    reconcile from the sync interval to KEYCLOAK_RECONCILE_CEILING less KEYCLOAK_RECONCILE_MARGIN less the sync interval
    (it runs at the first pass after it is due); the scheduler tick from 1 to 60; the overdue grace from
    OVERDUE_GRACE_TICKS ticks to OVERDUE_GRACE_CEILING; the shutdown grace under SHUTDOWN_GRACE_CEILING. ``got`` is what
    to quote, by variable name (the raw strings at load; the values themselves for a Config built by hand)."""
    raw = dict(got or {})

    def quote(name: str, value: Any) -> Any:
        return raw.get(name, value)

    problems = []
    sync_ok = _seconds(sync) and INTERVAL_FLOOR <= sync <= SYNC_INTERVAL_CEILING
    if not sync_ok:
        problems.append(SYNC_INTERVAL_RULE.format(got=quote("INKCAST_KEYCLOAK_SYNC_INTERVAL", sync)))
    # Judged against a sync interval within its rule: one outside it is the problem, not the order.
    floor = sync if sync_ok else 0
    # It runs at the first pass after it is due, up to one sync interval late; the margin is for its own run and Prometheus.
    limit = KEYCLOAK_RECONCILE_CEILING - KEYCLOAK_RECONCILE_MARGIN - floor
    if not (_seconds(reconcile) and floor <= reconcile <= limit):
        problems.append(RECONCILE_INTERVAL_RULE.format(sync=_show(quote("INKCAST_KEYCLOAK_SYNC_INTERVAL", sync)),
                                                       limit=_show(limit),
                                                       got=quote("INKCAST_KEYCLOAK_RECONCILE_INTERVAL", reconcile)))
    scheduler_ok = _seconds(scheduler) and SCHEDULER_INTERVAL[0] <= scheduler <= SCHEDULER_INTERVAL[1]
    if not scheduler_ok:
        problems.append(SCHEDULER_INTERVAL_RULE.format(got=quote("INKCAST_SCHEDULER_INTERVAL", scheduler)))
    twice = OVERDUE_GRACE_TICKS * (scheduler if scheduler_ok else SCHEDULER_INTERVAL[0])
    if not (_seconds(grace) and twice <= grace <= OVERDUE_GRACE_CEILING):
        problems.append(OVERDUE_GRACE_RULE.format(twice=_show(twice), got=quote("INKCAST_OVERDUE_GRACE", grace)))
    if not (_seconds(insights) and insights >= INTERVAL_FLOOR):
        problems.append(SECONDS_FLOOR_RULE.format(name="INKCAST_INSIGHTS_INTERVAL", got=quote("INKCAST_INSIGHTS_INTERVAL", insights)))
    if not (_seconds(shutdown) and shutdown < SHUTDOWN_GRACE_CEILING):
        problems.append(SHUTDOWN_GRACE_RULE.format(got=quote("INKCAST_SHUTDOWN_GRACE", shutdown)))
    return problems


def rate_limit_problems(per_minute: Any, got: Any = None) -> list[str]:
    """The rule of INKCAST_RATE_LIMIT_PER_MINUTE (a whole number from 0 to RATE_LIMIT_CEILING), as a problem naming it.
    0 turns the limit off, which is a posture check (rate-limit), not a refusal: a negative only looked like a setting.
    The ceiling is there because the limiter holds the bucket as a float: a value no float can hold was a 500 on every
    rate-limited request (#157)."""
    if _whole(per_minute) and 0 <= per_minute <= RATE_LIMIT_CEILING:
        return []
    return [RATE_LIMIT_RULE.format(got=per_minute if got is None else got)]


def backup_keep_problems(keep: Any, got: Any = None) -> list[str]:
    """The rule of INKCAST_BACKUP_KEEP (a whole number, 1 or more), as a problem naming it: the server (the deletion
    pages quote it) and ``inkcast backup`` (the CronJob) read it through this one rule. Above 35 is a posture warning
    (backup-keep, libs/secrets/checks), not a refusal."""
    if _whole(keep) and keep >= 1:
        return []
    return [BACKUP_KEEP_RULE.format(got=keep if got is None else got)]


def backup_keep_from(environ: Mapping[str, str]) -> int:
    """INKCAST_BACKUP_KEEP as ``inkcast backup`` reads it: the same rule as the server's, a ConfigError naming the
    variable instead of a traceback, and the CronJob's default (libs/db/backup.DEFAULT_KEEP, 14) when unset."""
    return _Env(environ).backup_keep()


def _problems(problems: list[str]) -> None:
    """Raise the problems found at load, one plainly or several as a list."""
    if len(problems) == 1:
        raise ConfigError(problems[0])
    if problems:
        raise ConfigError("Inkcast configuration problems:\n- " + "\n- ".join(problems))


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

    def publish_recovery_max_hours(self) -> int:
        """INKCAST_PUBLISH_RECOVERY_MAX_HOURS, a whole number of hours from 1 to 168; anything else is a ConfigError naming it."""
        raw = self.get("INKCAST_PUBLISH_RECOVERY_MAX_HOURS", "24")
        try:
            hours: Any = int(raw)
        except ValueError:
            hours = raw                               # fails the whole-number rule below, with the same message
        _problems(publish_recovery_problems(hours, got=raw))
        return hours

    def on_off(self, name: str, default: str) -> bool:
        """``on`` or ``off`` and nothing else: a safety switch must not read a typo as either."""
        raw = self.get(name, default)
        if raw.lower() not in ("on", "off"):
            raise ConfigError(ON_OFF_RULE.format(name=name, got=raw))
        return raw.lower() == "on"

    def int_in(self, name: str, default: str, low: int, high: int | None, rule: str) -> int:
        """A whole number from ``low`` to ``high`` (no upper bound when None); anything else is ``rule``."""
        raw = self.get(name, default)
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(rule.format(got=raw)) from None
        if value < low or (high is not None and value > high):
            raise ConfigError(rule.format(got=raw))
        return value
    def pat_policy(self) -> PatPolicy:
        """INKCAST_PAT_DEFAULT_DAYS, INKCAST_PAT_MAX_DAYS and INKCAST_PAT_ALLOW_NO_EXPIRY as a ``PatPolicy``; a value
        outside its rule is a ConfigError naming that variable (each of them, when several are wrong)."""
        raw_default = self.get("INKCAST_PAT_DEFAULT_DAYS", "90")
        raw_max = self.get("INKCAST_PAT_MAX_DAYS", str(PAT_DAYS_CEILING))
        raw_allow = self.get("INKCAST_PAT_ALLOW_NO_EXPIRY", "true")
        try:
            default: Any = int(raw_default)
        except ValueError:
            default = raw_default                     # fails the whole-number rule below, with the same message
        try:
            maximum: Any = int(raw_max)
        except ValueError:
            maximum = raw_max
        # Not _Env.bool: its "anything else is false" would turn a typo into a silently stricter policy.
        policy = PatPolicy(default, maximum, _SWITCH.get(raw_allow.lower(), raw_allow))
        _problems(pat_policy_problems(policy, got=(raw_default, raw_max, raw_allow)))
        return policy

    def refresh_token_days(self) -> int:
        """INKCAST_REFRESH_TOKEN_DAYS, a whole number of days from 1 to 90; anything else is a ConfigError naming it."""
        raw = self.get("INKCAST_REFRESH_TOKEN_DAYS", "30")
        try:
            days: Any = int(raw)
        except ValueError:
            days = raw
        _problems(refresh_token_problems(days, got=raw))
        return days

    def one_time_link_minutes(self) -> int:
        """INKCAST_ONE_TIME_LINK_MINUTES, a whole number of minutes from 5 to 60; anything else is a ConfigError naming it."""
        raw = self.get("INKCAST_ONE_TIME_LINK_MINUTES", str(ONE_TIME_LINK_DEFAULT_MINUTES))
        try:
            minutes: Any = int(raw)
        except ValueError:
            minutes = raw
        _problems(one_time_link_problems(minutes, got=raw))
        return minutes

    def intervals(self) -> dict[str, float]:
        """The six interval settings (INTERVAL_SETTINGS), by variable name, with their defaults; a value outside its rule
        is a ConfigError naming that variable (each of them, when several are wrong). A non-number is quoted as typed."""
        defaults = ("30", "600", "20", "600", "10", "25")
        raw = {name: self.get(name, default) for name, default in zip(INTERVAL_SETTINGS, defaults)}
        values: dict[str, Any] = {}
        for name, text in raw.items():
            try:
                values[name] = float(text)
            except ValueError:
                values[name] = text                   # fails the number rule below, with the same message
        sync, reconcile, scheduler, grace, insights, shutdown = (values[name] for name in INTERVAL_SETTINGS)
        _problems(interval_problems(sync=sync, reconcile=reconcile, scheduler=scheduler, grace=grace, insights=insights,
                                    shutdown=shutdown, got=raw))
        return values

    def rate_limit(self) -> int:
        """INKCAST_RATE_LIMIT_PER_MINUTE, a whole number, 0 (off) or more; anything else is a ConfigError naming it."""
        raw = self.get("INKCAST_RATE_LIMIT_PER_MINUTE", "60")
        try:
            per_minute: Any = int(raw)
        except ValueError:
            per_minute = raw
        _problems(rate_limit_problems(per_minute, got=raw))
        return per_minute

    def backup_keep(self) -> int:
        """INKCAST_BACKUP_KEEP, a whole number, 1 or more; anything else is a ConfigError naming it."""
        raw = self.get("INKCAST_BACKUP_KEEP", "14")
        try:
            keep: Any = int(raw)
        except ValueError:
            keep = raw
        _problems(backup_keep_problems(keep, got=raw))
        return keep

    def retention(self) -> RetentionPolicy:
        """INKCAST_OUTBOX_RETENTION_DAYS and INKCAST_KPI_HOURLY_RETENTION_DAYS as a ``RetentionPolicy``; a value outside its
        rule, or an hourly retention shorter than the outbox's, is a ConfigError naming the variable (each, when several
        are wrong)."""
        raw_outbox = self.get("INKCAST_OUTBOX_RETENTION_DAYS", "7")
        raw_hourly = self.get("INKCAST_KPI_HOURLY_RETENTION_DAYS", "90")
        try:
            outbox: Any = int(raw_outbox)
        except ValueError:
            outbox = raw_outbox                       # fails the whole-number rule below, with the same message
        try:
            hourly: Any = int(raw_hourly)
        except ValueError:
            hourly = raw_hourly
        policy = RetentionPolicy(outbox, hourly)
        _problems(retention_problems(policy, got=(raw_outbox, raw_hourly)))
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
    # How often admin events are read (INKCAST_KEYCLOAK_SYNC_INTERVAL, 1 to 840; 60 s or more is a posture warning: the
    # "within a minute" promise), and how often every Keycloak-backed user is re-checked for missed events
    # (INKCAST_KEYCLOAK_RECONCILE_INTERVAL, from the sync interval to 1680 less it, since it runs at the first pass after
    # it is due: the InkcastIdentitySyncPersonFailing alert's 30-minute window, less a 120 s margin). F1.10.S5, #93, #157.
    keycloak_sync_interval_seconds: float = 30.0
    keycloak_reconcile_seconds: float = 600.0
    # Days a person Keycloak no longer has (an administrator's deletion, or a self-deletion whose event was never
    # seen) stays cut off before the reconcile erases their data (E9.F9.5.S6, issue #86): 0 erases at the first
    # reconcile that finds them gone; None (INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=off) never erases on a 404, which is a
    # posture warning in production and staging. The grace period lets an accidental deletion be restored.
    keycloak_erase_after_days: int | None = 30
    # The sync's circuit breaker (E9.F9.5.S7, issue #105; S8, #135): a reconcile that finds at least keycloak_breaker_min
    # people newly gone from Keycloak (404) or newly disabled in it (enabled: false), and more than
    # keycloak_breaker_percent of those it read, cuts off and erases nobody and starts no grace period; the disabled
    # lose their OAuth tokens and keep their personal access tokens, refused; it records the pass for an operator to
    # confirm or dismiss (`inkcast identity`). A Keycloak restored from an older backup, the issuer pointing at the
    # wrong Keycloak, or a user-storage provider switched off all answer 404 (or disabled, for an importing provider)
    # about people who still exist. off (INKCAST_KEYCLOAK_BREAKER=off) is a posture warning in production and staging.
    keycloak_breaker: bool = True
    keycloak_breaker_percent: int = 10
    keycloak_breaker_min: int = 5

    # An OAuth access token (an MCP client's) lasts an hour. Deliberately not configurable: it is an opaque token
    # checked in the database on every call, so cutting someone off (a revocation, Keycloak disabling them) is
    # immediate whatever its lifetime, and a longer one would only widen what a leaked token is good for.
    access_token_seconds: int = 3600
    # How long a newly issued refresh token lasts (INKCAST_REFRESH_TOKEN_DAYS, 1-90; F1.10.S2, issue #90). Each
    # refresh rotates and issues a new one with the full lifetime, so this is an idle timeout: an MCP client left
    # unused this long signs in again. Refresh tokens already issued keep the lifetime they were given.
    refresh_token_days: int = 30
    # Personal access tokens (F1.10.S2, issue #90): INKCAST_PAT_DEFAULT_DAYS when the request doesn't say, at most
    # INKCAST_PAT_MAX_DAYS (never above 366), and never only while INKCAST_PAT_ALLOW_NO_EXPIRY (a posture info in
    # production). Enforced where a token is made; tokens already issued keep their expiry.
    pat: PatPolicy = PatPolicy()
    # One-time links (F1.10.S4, issue #92): minutes a connect link (inkcast_connect, POST /api/v1/platforms/{id}/connect-link)
    # and an account-deletion link (inkcast_delete_account, POST /api/v1/account/deletion-link) last, one value for both
    # (INKCAST_ONE_TIME_LINK_MINUTES, 5-60). 60 is a hard maximum: a connect link is a bearer capability that passes
    # through the AI transcript, and whoever holds it can attach their own platform account to the person's Inkcast.
    # The replies state it (expires_in, "valid N minutes"). Handed to the publishing use cases and libs/account; the
    # sign-in and return windows (libs/account.RETURN_TTL, the consent page) are protocol windows and stay constant.
    one_time_link_minutes: int = ONE_TIME_LINK_DEFAULT_MINUTES
    scheduler_interval_seconds: float = 20.0  # INKCAST_SCHEDULER_INTERVAL, 1-60: 120 s or more fails the on-time KPI by design
    # How a scheduled post whose send certainly never happened is retried (F1.10.S1, issue #89):
    # INKCAST_PUBLISH_MAX_ATTEMPTS sends in all, INKCAST_PUBLISH_RETRY_DELAYS seconds apart (the last repeats).
    # The default is 4 sends over about 36 minutes. A window over 24 h is a posture warning (publish-retry-window).
    publish_retry: RetryPolicy = RetryPolicy()
    # Hours a post whose send was interrupted (the process stopped mid-send) stays 'recovering' while the platform
    # refuses the lookup that would tell whether it arrived (INKCAST_PUBLISH_RECOVERY_MAX_HOURS, 1-168; #216). Past the
    # cap it is 'unconfirmed' for a person: it is never sent late on its own. The InkcastPostRecoveringTooLong alert
    # fires after an hour of it. Handed to the Publisher as seconds (publish_recovery_max_seconds).
    publish_recovery_max_hours: int = 24
    insights_interval_seconds: float = 10.0  # how often InsightsService folds new domain events into KPI rollups (1 or more)
    # Retention (F1.10.S3, issue #91): INKCAST_OUTBOX_RETENTION_DAYS (7, 1-30) keeps domain events every consumer has
    # passed, the replay window; INKCAST_KPI_HOURLY_RETENTION_DAYS (90, 31-400, never shorter than the outbox's) keeps
    # hourly KPI buckets and the kpi_applied rows that make a replay harmless. Daily buckets are kept. The scheduler's
    # housekeeping hands it to Store.purge_expired every 90 ticks.
    retention: RetentionPolicy = RetentionPolicy()
    log_format: str = "text"  # "json" in containers
    log_level: str = "INFO"
    metrics_port: int = 9090  # Prometheus /metrics on its own port; 0 disables
    shutdown_grace_seconds: float = 25.0  # INKCAST_SHUTDOWN_GRACE, positive and under the Deployment's terminationGracePeriodSeconds (40)
    max_body_bytes: int = 10 * 1024 * 1024  # a request body above this is refused with 413 before any handler runs
    # Writes per minute per client address on the endpoints anyone can call without a token (client
    # registration, the token page, the password form, the connect page), 0 to 10000. 0 disables the limit, which with
    # password sign-in is unbounded password guessing: a posture failure in production (rate-limit); above 600 with
    # password sign-in (ten guesses a second) is a posture warning. Never negative.
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
    # INKCAST_FGA_STORE_NAME (E3.F3.9.S6): with both ids unset, start-up resolves the store by this name and pins
    # the version equal to the packaged model (libs/authz pin_model), refusing to start until the writer has written it.
    fga_store_name: str = ""
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
    # Scheduled posts this long past due mean the scheduler is behind or stopped (alert + readiness). At least two
    # scheduler intervals: below that, a post due between ticks is "overdue" and InkcastPostsOverdue fires on a healthy
    # system. At most an hour: beyond that the alert stays quiet about a stopped scheduler that long.
    overdue_grace_seconds: float = 600.0
    # How many daily backups the backup CronJob keeps (its INKCAST_BACKUP_KEEP; libs/db/backup.DEFAULT_KEEP): the
    # deletion pages tell a person that copies of their data in backups age out within this many days (issue #94).
    # The server reads the same name, so a changed CronJob value goes in the shared ConfigMap too. 1 or more; above
    # 35 (checks.BACKUP_KEEP_WARN) a deleted person's data lingers in backups over five weeks, a posture warning (backup-keep).
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
    def one_time_link_seconds(self) -> int:
        """What a connect or deletion ticket is made with, and what the replies' ``expires_in`` says."""
        return self.one_time_link_minutes * 60

    @property
    def publish_recovery_max_seconds(self) -> int:
        """The recovery cap as the Publisher measures it (libs/pipeline ``recovery_max``)."""
        return self.publish_recovery_max_hours * 3600

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
        problems += backup_keep_problems(self.backup_keep)      # load_config refuses already; a Config built by hand
        problems += rate_limit_problems(self.rate_limit_per_minute)
        problems += interval_problems(sync=self.keycloak_sync_interval_seconds, reconcile=self.keycloak_reconcile_seconds,
                                      scheduler=self.scheduler_interval_seconds, grace=self.overdue_grace_seconds,
                                      insights=self.insights_interval_seconds, shutdown=self.shutdown_grace_seconds)
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
        if not isinstance(self.keycloak_breaker, bool):
            problems.append(ON_OFF_RULE.format(name="INKCAST_KEYCLOAK_BREAKER", got=self.keycloak_breaker))
        percent, minimum = self.keycloak_breaker_percent, self.keycloak_breaker_min
        if not (_whole(percent) and 1 <= percent <= 100):
            problems.append(BREAKER_PERCENT_RULE.format(got=percent))
        if not (_whole(minimum) and minimum >= 1):
            problems.append(BREAKER_MIN_RULE.format(got=minimum))
        problems += publish_retry_problems(self.publish_retry)  # load_config refuses already; a Config built by hand
        problems += publish_recovery_problems(self.publish_recovery_max_hours)   # likewise
        problems += pat_policy_problems(self.pat)              # likewise
        problems += refresh_token_problems(self.refresh_token_days)
        problems += one_time_link_problems(self.one_time_link_minutes)
        problems += retention_problems(self.retention)
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
            # Each missing one by name: a deployment whose ids are not pinned yet (E3.F3.9.S1) must read what to pin.
            # The store name (E3.F3.9.S6) stands in for the pair of ids, never for half of it: with both unset, the
            # pod resolves them at start-up (libs/authz pin_model); explicit ids win and are not looked up.
            missing = ["INKCAST_FGA_API_URL"] if not self.fga_api_url else []
            unset = [name for name, value in (("INKCAST_FGA_STORE_ID", self.fga_store_id),
                                              ("INKCAST_FGA_MODEL_ID", self.fga_model_id)) if not value]
            if len(unset) == 1 or (unset and not self.fga_store_name):
                missing += unset
            if missing:
                names = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + " and " + missing[-1]
                if len(unset) == 2 and not self.fga_store_name:
                    how = (f" Or set INKCAST_FGA_STORE_NAME instead of both ids: {AUTHZ_MODEL_WRITER} writes the "
                           "packaged model to that store, and start-up pins the version equal to it.")
                elif len(unset) == 1 and self.fga_store_name:
                    how = " INKCAST_FGA_STORE_NAME stands in for the pair only, with both ids unset."
                else:
                    how = ""
                problems.append(f"INKCAST_AUTHZ_ENGINE=openfga needs {names} (the server, the store and the pinned "
                                f"model version; `inkcast authz write-model` prints the store and model ids).{how}")
            if self.fga_store_name and not FGA_STORE_NAME.fullmatch(self.fga_store_name):
                problems.append("INKCAST_FGA_STORE_NAME must be 3 to 64 characters of letters, digits, spaces and "
                                ". - / ^ _ & @ (OpenFGA's store-name rule).")
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
    intervals = env.intervals()
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
        keycloak_sync_interval_seconds=intervals["INKCAST_KEYCLOAK_SYNC_INTERVAL"],
        keycloak_reconcile_seconds=intervals["INKCAST_KEYCLOAK_RECONCILE_INTERVAL"],
        keycloak_erase_after_days=env.days_or_off("INKCAST_KEYCLOAK_ERASE_AFTER_DAYS", "30"),
        keycloak_breaker=env.on_off("INKCAST_KEYCLOAK_BREAKER", "on"),
        keycloak_breaker_percent=env.int_in("INKCAST_KEYCLOAK_BREAKER_PERCENT", "10", 1, 100, BREAKER_PERCENT_RULE),
        keycloak_breaker_min=env.int_in("INKCAST_KEYCLOAK_BREAKER_MIN", "5", 1, None, BREAKER_MIN_RULE),
        refresh_token_days=env.refresh_token_days(),
        pat=env.pat_policy(),
        one_time_link_minutes=env.one_time_link_minutes(),
        extra_allowed_hosts=tuple(h.strip() for h in settings.get("INKCAST_ALLOWED_HOSTS", "").split(",") if h.strip()),
        platform_urls=env.prefixed("INKCAST_API_URL_", str.lower),
        app_name=env.get("INKCAST_APP_NAME", "All"),
        path_prefix=env.get("INKCAST_PATH_PREFIX", "/"),
        upstreams=env.prefixed("INKCAST_UPSTREAM_", lambda k: k.lower().replace("_", "-")),
        overdue_grace_seconds=intervals["INKCAST_OVERDUE_GRACE"],
        backup_keep=env.backup_keep(),
        scheduler_interval_seconds=intervals["INKCAST_SCHEDULER_INTERVAL"],
        publish_retry=env.publish_retry(),
        publish_recovery_max_hours=env.publish_recovery_max_hours(),
        insights_interval_seconds=intervals["INKCAST_INSIGHTS_INTERVAL"],
        retention=env.retention(),
        log_format=env.get("INKCAST_LOG_FORMAT", "text").lower(),
        log_level=env.get("INKCAST_LOG_LEVEL", "INFO"),
        metrics_port=env.int("INKCAST_METRICS_PORT", "9090"),
        shutdown_grace_seconds=intervals["INKCAST_SHUTDOWN_GRACE"],
        max_body_bytes=env.int("INKCAST_MAX_BODY_BYTES", str(10 * 1024 * 1024)),
        rate_limit_per_minute=env.rate_limit(),
        cache_url=cache_url,
        # Asked for only when a shared cache is configured: no credential is fetched that nothing uses.
        cache_password=secret("INKCAST_CACHE_PASSWORD") if cache_url else SecretValue(""),
        authz_engine=authz_engine,
        authz_allow_local=env.bool("INKCAST_AUTHZ_ALLOW_LOCAL"),
        authz_model_path=env.get("INKCAST_AUTHZ_MODEL"),
        fga_api_url=env.get("INKCAST_FGA_API_URL").rstrip("/"),
        fga_store_id=env.get("INKCAST_FGA_STORE_ID"),
        fga_model_id=env.get("INKCAST_FGA_MODEL_ID"),
        fga_store_name=env.get("INKCAST_FGA_STORE_NAME").strip(),
        # Asked for only when OpenFGA is the engine: no credential is fetched that nothing uses.
        fga_api_token=secret("INKCAST_FGA_API_TOKEN") if authz_engine == "openfga" else SecretValue(""),
        fga_timeout_seconds=env.float("INKCAST_FGA_TIMEOUT", "5"),
        reveal_secrets=env.bool("INKCAST_LOG_REVEAL_SECRETS"),
        secrets=provider,
        secret_errors=secret_errors,
    )
