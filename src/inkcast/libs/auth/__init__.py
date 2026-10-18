# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Who is calling, and what they may do. Shared by every service that serves an API.

Two kinds of bearer token reach Inkcast:

* OAuth access tokens (``ic_at_…``) issued by IdentityService to MCP clients after the person
  signs in and consents. They carry the ``inkcast`` scope, which grants every scope below.
* Personal access tokens (``ink_pat_<id>_<secret>``) created by the person for CI and scripts.
  They carry only the scopes chosen when they were created, and can never create other tokens.

``TokenVerifier`` turns either into a ``Principal``. It also implements the MCP SDK's
``TokenVerifier`` protocol, so the ``/mcp`` endpoint and the REST API accept the same tokens.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from mcp.server.auth.provider import AccessToken

from ..db import IdentityRepo

SCOPE = "inkcast"  # the OAuth scope; implies every fine-grained scope

SCOPES: dict[str, str] = {
    "posts:read": "List posts and preview stories.",
    "posts:write": "Publish, schedule and cancel posts.",
    "stories:sync": "Sync markdown files from a repository (create, update or skip by content).",
    "connections:read": "See which platforms are connected and list their destinations.",
    "connections:write": "Create connect links and disconnect platforms.",
}

PAT_PREFIX = "ink_pat_"


@dataclass(frozen=True)
class Principal:
    user_id: str
    scopes: frozenset[str]
    kind: Literal["oauth", "pat"]
    client_id: str
    token_id: str | None = None

    def can(self, scope: str) -> bool:
        return scope in self.scopes


class TokenVerifier:
    def __init__(self, store: IdentityRepo):
        self.store = store

    def lookup(self, token: str) -> tuple[Principal, dict] | None:
        """One store read: (principal, its token row), or None for anything that is not a live token.
        Lookups are by SHA-256 hash, so comparing a presented token is a key match, not a timing side channel."""
        if not token:
            return None
        if token.startswith(PAT_PREFIX):
            row = self.store.find_api_token(token)
            if not row or not self.store.get_user(row["user_id"]):
                return None
            return Principal(row["user_id"], frozenset(row["scopes"].split()) & SCOPES.keys(), "pat",
                             client_id=f"pat:{row['id']}", token_id=row["id"]), row
        row = self.store.get_token(token, "access")
        if not row or not self.store.get_user(row["user_id"]):
            return None
        granted = set(row["scopes"].split())
        scopes = set(SCOPES) if SCOPE in granted else granted & SCOPES.keys()
        return Principal(row["user_id"], frozenset(scopes), "oauth", client_id=row["client_id"]), row

    def principal(self, token: str) -> Principal | None:
        found = self.lookup(token)
        return found[0] if found else None

    def access_token(self, token: str) -> AccessToken | None:
        """The MCP SDK's view of a token. PATs get the ``inkcast`` gate scope so ``/mcp`` admits them;
        what each tool may do is still decided by the principal's own scopes."""
        found = self.lookup(token)
        if found is None:
            return None
        p, row = found
        if p.kind == "pat":
            expires = row.get("expires_at")
            return AccessToken(token=token, client_id=p.client_id, scopes=[SCOPE, *sorted(p.scopes)],
                               expires_at=int(expires) if expires else None, subject=p.user_id)
        return AccessToken(token=token, client_id=p.client_id, scopes=row.get("scopes", "").split(),
                           expires_at=int(row.get("expires_at", time.time())), resource=row.get("resource"),
                           subject=p.user_id)

    async def verify_token(self, token: str) -> AccessToken | None:  # MCP TokenVerifier protocol
        return self.access_token(token)


__all__ = ["PAT_PREFIX", "SCOPE", "SCOPES", "Principal", "TokenVerifier"]
