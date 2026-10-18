"""Platform plug-in registry: built-ins plus anything registered under the
``inkcast.platforms`` entry point group."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

import httpx2

from .base import Account, CredentialField, Destination, Platform, PlatformError, Prepared, PublishResult
from .devto import DevtoPlatform
from .medium import MediumPlatform

log = logging.getLogger(__name__)

BUILTIN: dict[str, type[Platform]] = {cls.id: cls for cls in (MediumPlatform, DevtoPlatform)}


def discover() -> dict[str, type[Platform]]:
    found = dict(BUILTIN)
    for ep in entry_points(group="inkcast.platforms"):
        try:
            cls = ep.load()
        except Exception:  # a broken plug-in must not take the server down
            log.exception("Could not load platform plug-in %r", ep.name)
            continue
        if not (isinstance(cls, type) and issubclass(cls, Platform)):
            log.error("Entry point %r is not a Platform subclass; skipped", ep.name)
            continue
        if cls.id in found and found[cls.id] is not cls:
            log.warning("Platform plug-in %r overrides the built-in %r", ep.name, cls.id)
        found[cls.id] = cls
    return found


def load_platforms(http: httpx2.AsyncClient, api_urls: dict[str, str] | None = None) -> dict[str, Platform]:
    api_urls = api_urls or {}
    return {pid: cls(http, api_urls.get(pid)) for pid, cls in sorted(discover().items())}


__all__ = [
    "Account", "CredentialField", "Destination", "Platform", "PlatformError", "Prepared", "PublishResult",
    "BUILTIN", "discover", "load_platforms",
]
