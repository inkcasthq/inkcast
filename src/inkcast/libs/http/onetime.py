# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""One-time pages (/connect/<ticket>, /account/delete/<ticket>): a GET, or the HEAD Starlette adds to every GET
route, only shows the page; the POST from the page's button uses the link up (issue #401).

Safe methods are what link previewers, mail scanners and prefetchers send (RFC 9110 §9.2.1), so a page reached by
one must change nothing, or the person finds the link spent before they open it. The page writes no row and sets no
cookie: a previewer leaves nothing behind.

The POST carries a token bound to the ticket: HMAC-SHA256 under a key derived from INKCAST_SECRET_KEY for this one
purpose, over the ticket. Only a form Inkcast served for that very ticket can answer, so a blind POST (a scanner that
tries POST too, a form on another site, a token taken from another link) is refused before anything is started, and
SameSite cookies are not the only defence (OWASP, CSRF Prevention Cheat Sheet; these pages set no cookie at all).
Stateless on purpose: the same ticket gives the same token, so the form shown again after a refused key carries a
token that still works. Whoever holds the link can read its page and its token: the link is the capability, bounded
by its lifetime (INKCAST_ONE_TIME_LINK_MINUTES) and its single use."""

from __future__ import annotations

import base64
import hashlib
import hmac

from ..secrets import SecretValue, derive_key


class PageTokens:
    """The token a one-time page's form carries, and its check."""

    def __init__(self, secret_key: str | SecretValue):
        self._key = derive_key(secret_key, "one-time-page")

    def make(self, ticket: str) -> str:
        digest = hmac.new(self._key, ticket.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def ok(self, ticket: str, given: object) -> bool:
        """Compared in constant time, never with ``!=``; a missing or empty value is simply wrong."""
        return hmac.compare_digest(self.make(ticket).encode(), str(given or "").encode())


__all__ = ["PageTokens"]
