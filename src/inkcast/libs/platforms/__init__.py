# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
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
NAME_MAX = 40  # characters in a platform's name, which Inkcast writes into every note on that platform's posts


def _writable(name: object) -> bool:
    """Whether a plug-in's ``name`` can be written into a post's notes, which name their platform: text of 1 to NAME_MAX
    printable characters, without ";". The notes are stored joined with "; " (``PostsRepo.join_notes``), and a ";" in
    the name, before a note's next word, would split a note into pieces, none of them the note: a reader that recognises
    it whole (``_is_second_copy``, ``_edited``) would miss it (#533). A name that is empty, long, or holds a newline or
    another character that isn't printable has no place in the sentence a person reads either."""
    return isinstance(name, str) and 0 < len(name) <= NAME_MAX and name.isprintable() and ";" not in name


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
        if not _writable(getattr(cls, "name", None)):  # the name itself is the plug-in's text, never logged
            log.error("Entry point %r is skipped: a platform's name is 1 to %d printable characters, without \";\" (Inkcast "
                      "writes it into a post's notes, which are joined with \"; \")", ep.name, NAME_MAX)
            continue
        if cls.id in found and found[cls.id] is not cls:
            log.warning("Platform plug-in %r overrides the built-in %r", ep.name, cls.id)
        found[cls.id] = cls
    return found


def load_platforms(http: httpx2.AsyncClient, api_urls: dict[str, str] | None = None) -> dict[str, Platform]:
    """One instance per platform ``discover()`` found. Every note reads the instance's ``name``, which ``__init__`` may
    set, so it is held to the rule ``discover()`` holds the class's to: a plug-in whose instance breaks it is skipped,
    and a built-in it would override stays (#533)."""
    api_urls = api_urls or {}
    loaded: dict[str, Platform] = {}
    for pid, cls in sorted(discover().items()):
        platform = cls(http, api_urls.get(pid))
        if not _writable(getattr(platform, "name", None)):  # the plug-in's text, never logged, as in ``discover``
            log.error("Platform %r is skipped: a platform's name is 1 to %d printable characters, without \";\" (Inkcast "
                      "writes it into a post's notes, which are joined with \"; \")", pid, NAME_MAX)
            if pid not in BUILTIN:
                continue
            platform = BUILTIN[pid](http, api_urls.get(pid))
        loaded[pid] = platform
    return loaded


__all__ = [
    "Account", "CredentialField", "Destination", "Platform", "PlatformError", "Prepared", "PublishResult",
    "BUILTIN", "discover", "load_platforms",
]
