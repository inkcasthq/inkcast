# SPDX-License-Identifier: PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Inkcast's OAuth 2.1 authorization server for MCP clients.

Claude Code (or any MCP client) registers itself dynamically, sends the person
to /authorize with PKCE, and gets tokens bound to an Inkcast user. *Who* the
person is comes from the configured identity provider (identity.py); every
client is shown a consent page before a code is issued, so a dynamically
registered client can never get a token silently.
"""

from __future__ import annotations

import json
import uuid
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ..config import Config
from ..storage import Store, new_secret

SCOPE = "inkcast"


class InkcastOAuthProvider:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    # ---- clients (RFC 7591 dynamic registration) --------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.save_client(client_info.client_id, client_info.model_dump_json())

    # ---- authorization --------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pending_id = self.store.create_pending(client.client_id, params.model_dump_json())
        return f"{self.config.public_url}/auth/login?pending={pending_id}"

    def describe_pending(self, pending: dict) -> tuple[str, str]:
        """(client name, redirect host) for the consent page."""
        info = self.store.get_client(pending["client_id"])
        name = (json.loads(info).get("client_name") if info else None) or "An MCP client"
        params = AuthorizationParams.model_validate_json(pending["params"])
        parsed = urlparse(str(params.redirect_uri))
        return name, parsed.netloc or parsed.scheme

    def approve(self, pending: dict) -> str:
        """Issue an authorization code for an approved pending request; return the client redirect."""
        params = AuthorizationParams.model_validate_json(pending["params"])
        code = new_secret("ic_code_")
        self.store.save_code(code, pending["client_id"], pending["user_id"], pending["params"])
        self.store.delete_pending(pending["id"])
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, pending: dict) -> str:
        params = AuthorizationParams.model_validate_json(pending["params"])
        self.store.delete_pending(pending["id"])
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        row = self.store.get_code(authorization_code)
        if not row or row["client_id"] != client.client_id:
            return None
        params = AuthorizationParams.model_validate_json(row["params"])
        return AuthorizationCode(
            code=authorization_code,
            scopes=params.scopes or [SCOPE],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=row["user_id"],
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if not self.store.consume_code(authorization_code.code):  # single use
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.subject or "", authorization_code.scopes,
                           authorization_code.resource, family=uuid.uuid4().hex)

    # ---- tokens ---------------------------------------------------------

    def _issue(self, client_id: str, user_id: str, scopes: list[str], resource: str | None, family: str) -> OAuthToken:
        access = new_secret("ic_at_")
        refresh = new_secret("ic_rt_")
        self.store.save_token(access, "access", client_id, user_id, scopes, resource, family, self.config.access_token_seconds)
        self.store.save_token(refresh, "refresh", client_id, user_id, scopes, resource, family, self.config.refresh_token_seconds)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.config.access_token_seconds,
                          scope=" ".join(scopes), refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.get_token(refresh_token, "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"].split(),
                            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["user_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        row = self.store.get_token(refresh_token.token, "refresh")
        if not row:
            raise TokenError("invalid_grant", "refresh token expired or revoked")
        # Rotate: the old refresh token (and access tokens of the family) stop working.
        self.store.revoke_family(row["family"])
        granted = [s for s in (scopes or refresh_token.scopes) if s in refresh_token.scopes] or refresh_token.scopes
        return self._issue(client.client_id, row["user_id"], granted, row["resource"], family=uuid.uuid4().hex)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.get_token(token, "access")
        if not row or not self.store.get_user(row["user_id"]):
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                           expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["user_id"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for kind in ("access", "refresh"):
            row = self.store.get_token(token.token, kind)
            if row:
                self.store.revoke_family(row["family"])

    def housekeeping(self) -> None:
        self.store.purge_expired()


__all__ = ["InkcastOAuthProvider", "SCOPE"]
