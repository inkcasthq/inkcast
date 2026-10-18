"""The plug-in interface every publishing platform implements.

A platform plug-in is a class with:

* identity: ``id`` (used in tool calls and front matter), ``name``
* ``credential_fields``: what the person pastes on the connect page
* ``verify``: check credentials and return the account they belong to
* ``prepare``: a PURE function turning a story into this platform's payload
  (used for dry runs and stored for scheduled posts)
* ``publish``: send a prepared payload

Optional: ``destinations`` / ``resolve_destination`` for publications,
organizations, blogs; ``not_connected_help`` for a manual fallback.

Third-party plug-ins register through the ``inkcast.platforms`` entry point
group; see docs/writing-a-platform-plugin.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

import httpx2


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


class PlatformError(Exception):
    """A failure worded for the person reading Claude's reply.

    ``retryable``: the request certainly did not take effect (connection never
    made, rate-limited), so the scheduler may safely try again.
    ``uncertain``: a publishing request may or may not have taken effect (timeout
    after sending, 5xx). Retrying could post twice, so the post is marked
    ``unknown`` for a human to check instead.
    """

    def __init__(self, message: str, *, retryable: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.retryable = retryable and not uncertain
        self.uncertain = uncertain


class Platform(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    default_api_url: ClassVar[str]
    credential_fields: ClassVar[list[CredentialField]]
    connect_help: ClassVar[str] = ""
    # What a destination is called on this platform ("publication", "organization", ...), or None.
    destination_label: ClassVar[str | None] = None

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
            raise PlatformError(f"{self.name} rate limit hit; try again shortly.", retryable=True)
        if response.status_code >= 500:
            if mutating:
                raise PlatformError(
                    f"{self.name} returned a server error ({response.status_code}) while publishing; it may or may "
                    f"not have been published. Check {self.name} before trying again.", uncertain=True)
            raise PlatformError(f"{self.name} had a server error ({response.status_code}).", retryable=True)
        if response.status_code >= 400:
            raise PlatformError(self.describe_error(response))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def describe_error(self, response: httpx2.Response) -> str:
        detail = response.text.strip()[:500] or response.reason_phrase
        return f"{self.name} rejected the request ({response.status_code}): {detail}"
