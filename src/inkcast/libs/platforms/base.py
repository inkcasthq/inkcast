# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The plug-in interface every publishing platform implements.

A platform plug-in is a class with:

* identity: ``id`` (used in tool calls and front matter), ``name``
* ``credential_fields``: what the person pastes on the connect page
* ``verify``: check credentials and return the account they belong to
* ``prepare``: a PURE function turning a story into this platform's payload
  (used for dry runs and stored for scheduled posts)
* ``publish``: send a prepared payload

Optional: ``destinations`` / ``resolve_destination`` for publications,
organizations, blogs; ``not_connected_help`` for a manual fallback;
``update`` (declare ``"update"`` in ``capabilities``) to edit a published post in
place, which Git publishing uses when a file changes; ``find_existing`` so an
uncertain publish can be checked before anyone retries it.

Third-party plug-ins register through the ``inkcast.platforms`` entry point
group; see docs/writing-a-platform-plugin.md.
"""

from __future__ import annotations

import math
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

import httpx2

_DELAY_SECONDS = re.compile(r"[0-9]+")  # Retry-After's delay-seconds form: ASCII digits only, no sign, no fraction
# The longest wait a Retry-After is read as (over 31 years): far past any retry policy's cap, so a longer value is capped
# like any other, and int() never parses the thousands of digits a hostile header can carry.
RETRY_AFTER_CEILING = 10 ** 9


@dataclass(frozen=True)
class CredentialField:
    key: str
    label: str
    secret: bool = True
    required: bool = True
    help: str = ""
    placeholder: str = ""


@dataclass
class Account:
    id: str
    name: str
    handle: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        return cls(**{k: data.get(k) for k in ("id", "name", "handle", "url")})


@dataclass
class Destination:
    id: str
    name: str
    url: str | None = None
    note: str | None = None  # e.g. "editor" / "writer"


@dataclass
class Prepared:
    """A story rendered for one platform. Must be JSON-serialisable (it is stored for scheduled posts)."""

    platform: str
    title: str
    payload: dict[str, Any]
    preview: dict[str, Any]  # human-readable fields shown in dry runs
    destination: str | None = None  # as requested (name/slug/id); resolved at publish time
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Prepared":
        return cls(**data)


@dataclass
class PublishResult:
    url: str | None
    remote_id: str | None
    status: str  # "published", "draft", "unlisted"
    notes: list[str] = field(default_factory=list)


def retry_after_seconds(value: str | None, now: float | None = None) -> int | None:
    """The wait a ``Retry-After`` header asks for, in whole seconds up to ``RETRY_AFTER_CEILING``, or None when there is
    no header or it can't be read. RFC 9110 §10.2.3 allows two forms: delay-seconds, a non-negative integer, and an
    HTTP-date, measured from ``now`` (a date already past is 0: the platform is ready). Anything else (a sign, a
    fraction, words, an impossible date) is None, and the retry policy's own delay applies."""
    if value is None:
        return None
    text = value.strip()
    if _DELAY_SECONDS.fullmatch(text):
        digits = text.lstrip("0") or "0"
        return int(digits) if len(digits) < len(str(RETRY_AFTER_CEILING)) else RETRY_AFTER_CEILING
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # the asctime form, or a "-0000" zone: an HTTP-date is always UTC
        when = when.replace(tzinfo=timezone.utc)
    try:
        wait = math.ceil(when.timestamp() - (time.time() if now is None else now))
    except (OverflowError, ValueError, OSError):
        return None
    return min(max(0, wait), RETRY_AFTER_CEILING)


def wait_text(seconds: int | float | None) -> str:
    """How long a platform asked to wait, for a person: "shortly" when it didn't say (or said 0), else "in 5 seconds",
    "in 15 minutes", "in 24 hours", "in 2 days", rounded up to the unit."""
    try:
        whole = math.ceil(seconds)
    except (TypeError, ValueError, OverflowError):  # None, nan, inf
        return "shortly"
    if whole <= 0:
        return "shortly"
    for unit, size, limit in (("second", 1, 120), ("minute", 60, 120 * 60), ("hour", 3600, 48 * 3600)):
        if whole < limit:
            break
    else:
        unit, size = "day", 86400
    count = math.ceil(whole / size)
    return f"in {count} {unit}" + ("" if count == 1 else "s")


class PlatformError(Exception):
    """A failure worded for the person reading Claude's reply.

    ``retryable``: the request certainly did not take effect (connection never
    made, rate-limited), so the scheduler may safely try again.
    ``uncertain``: a publishing request may or may not have taken effect (timeout
    after sending, 5xx). Retrying could post twice, so the post is marked
    ``unconfirmed`` for a human to check instead.
    ``retry_after``: the seconds a 429's ``Retry-After`` asked for, carried on a
    retryable error only; the scheduler waits at least that long, within its
    policy's largest delay (libs/config.RetryPolicy.delay_after).
    """

    def __init__(self, message: str, *, retryable: bool = False, uncertain: bool = False,
                 retry_after: int | float | None = None):
        super().__init__(message)
        self.retryable = retryable and not uncertain
        self.uncertain = uncertain
        self.retry_after = retry_after if self.retryable else None


class Platform(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    default_api_url: ClassVar[str]
    credential_fields: ClassVar[list[CredentialField]]
    connect_help: ClassVar[str] = ""
    # What a destination is called on this platform ("publication", "organization", ...), or None.
    destination_label: ClassVar[str | None] = None
    # What else the platform API allows: "update" (edit a published post in place),
    # "find" (look up a post this payload created, to confirm an uncertain publish).
    capabilities: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, http: httpx2.AsyncClient, api_url: str | None = None):
        self.http = http
        self.api_url = (api_url or self.default_api_url).rstrip("/")

    # ---- required ------------------------------------------------------

    @abstractmethod
    async def verify(self, creds: dict[str, str]) -> Account:
        """Return the account these credentials belong to, or raise PlatformError."""

    @abstractmethod
    def prepare(self, fields: dict[str, Any], body: str) -> Prepared:
        """Render a story for this platform. ``fields`` comes from Story.for_platform(). No I/O."""

    @abstractmethod
    async def publish(
        self, creds: dict[str, str], account: Account, prepared: Prepared, destination: Destination | None
    ) -> PublishResult:
        """Send a prepared story."""

    # ---- optional ------------------------------------------------------

    async def destinations(self, creds: dict[str, str], account: Account) -> list[Destination]:
        return []

    async def resolve_destination(self, creds: dict[str, str], account: Account, wanted: str) -> Destination:
        needle = wanted.strip().lower()
        options = await self.destinations(creds, account)
        for d in options:
            slug = (d.url or "").rstrip("/").rsplit("/", 1)[-1].lower()
            if needle in {d.id.lower(), d.name.lower(), slug, (d.url or "").rstrip("/").lower()}:
                return d
        listed = ", ".join(f"{d.name} ({d.id})" for d in options) or "none"
        raise PlatformError(f"No {self.destination_label or 'destination'} matching {wanted!r} on {self.name}. Options: {listed}.")

    async def update(self, creds: dict[str, str], account: Account, remote_id: str, prepared: Prepared,
                     destination: Destination | None) -> PublishResult:
        """Replace the content of a post published earlier. Only called when "update" is in ``capabilities``."""
        raise PlatformError(f"{self.name} doesn't let Inkcast edit published posts.")

    async def find_existing(self, creds: dict[str, str], account: Account, prepared: Prepared) -> PublishResult | None:
        """The post this payload already created, or None. Only called when "find" is in ``capabilities``."""
        return None

    def not_connected_help(self, fields: dict[str, Any]) -> str | None:
        """Guidance when the person has no credentials for this platform (e.g. a manual import path)."""
        return None

    # ---- helpers for plug-ins -------------------------------------------

    async def request(self, method: str, path: str, *, headers: dict[str, str], mutating: bool | None = None,
                      **kwargs: Any) -> Any:
        """Call the platform API. ``mutating`` (default: any non-GET) makes ambiguous failures
        ``uncertain`` instead of ``retryable``, so a story is never published twice."""
        mutating = method.upper() != "GET" if mutating is None else mutating
        try:
            response = await self.http.request(method, f"{self.api_url}{path}", headers=headers, **kwargs)
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout) as exc:
            raise PlatformError(f"Could not reach {self.name}: {exc}", retryable=True) from exc
        except httpx2.HTTPError as exc:
            if mutating:
                raise PlatformError(
                    f"The connection to {self.name} failed after the request was sent ({exc}); it may or may not "
                    f"have been published. Check {self.name} before trying again.", uncertain=True) from exc
            raise PlatformError(f"Could not reach {self.name}: {exc}", retryable=True) from exc
        if response.status_code == 429:
            asked = retry_after_seconds(response.headers.get("Retry-After"))
            # The ask is in the words too: a "now" publish is not retried, so the person (or assistant) reading
            # the reply decides when to try again (#188).
            raise PlatformError(f"{self.name} rate limit hit; try again {wait_text(asked)}.", retryable=True,
                                retry_after=asked)
        if response.status_code >= 500:
            if mutating:
                raise PlatformError(
                    f"{self.name} returned a server error ({response.status_code}) while publishing; it may or may "
                    f"not have been published. Check {self.name} before trying again.", uncertain=True)
            raise PlatformError(f"{self.name} had a server error ({response.status_code}).", retryable=True)
        if response.status_code >= 400:
            raise PlatformError(self.describe_error(response))
        if response.status_code == 303 and mutating:
            # See Other is the server's indirect answer (RFC 9110 §15.4.4): it acted on the request, and its result
            # is at the Location. So the post may be out: uncertain, never "nothing was published".
            raise PlatformError(
                f"{self.name} answered with a redirect (303) instead of the post; it may or may not have been "
                f"published. Check {self.name} before trying again.", uncertain=True)
        if response.status_code >= 300:
            # A redirect (the client never follows one): the request did not reach the API we meant, so nothing was
            # published and nothing will change on a retry. Definite, whatever the body says (#173).
            where = response.headers.get("Location")
            raise PlatformError(
                f"{self.name} redirected the request ({response.status_code}"
                + (f" to {where[:200]}" if where else "") + ") instead of answering it, so it never reached the "
                f"{self.name} API and nothing was published. Check the {self.name} API URL (INKCAST_API_URL_"
                f"{self.id.upper()}).")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def describe_error(self, response: httpx2.Response) -> str:
        detail = response.text.strip()[:500] or response.reason_phrase
        return f"{self.name} rejected the request ({response.status_code}): {detail}"
