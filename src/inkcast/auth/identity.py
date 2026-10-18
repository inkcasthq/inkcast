# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Who is signing in. Pluggable, like platforms:

* OIDCIdentity   — any OpenID Connect provider. Point it at a Keycloak realm and
                   Keycloak brokers Google, GitHub, Microsoft... and links accounts.
* GitHubIdentity — a GitHub OAuth app, for small teams without an IdP.
* PasswordIdentity — one shared password, for a single person running locally.

Each provider turns a browser round-trip into an ``Identity``; the OAuth
server (provider.py) never needs to know which one was used.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx2
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ..config import Config
from . import pages


class LoginError(Exception):
    """Sign-in failed or isn't allowed; the message is shown to the person."""


@dataclass
class Identity:
    key: str  # stable unique id, e.g. "oidc:https://sso.example.com/realms/inkcast#<sub>"
    display: str  # what we call them on pages, e.g. a username or email
    email: str | None = None
    name: str | None = None


class IdentityProvider(ABC):
    def __init__(self, config: Config, http: httpx2.AsyncClient):
        self.config = config
        self.http = http
        self.callback_url = f"{config.public_url}/auth/callback"

    @abstractmethod
    async def start(self, request: Request, pending_id: str) -> Response:
        """Begin sign-in for a pending authorization (redirect, or render a form)."""

    @abstractmethod
    async def finish(self, request: Request) -> tuple[str, Identity]:
        """Complete sign-in from the callback request; return (pending_id, identity)."""

    # A per-authorization secret derived from the server key, so PKCE verifiers
    # don't need their own storage.
    def _derived(self, label: str, pending_id: str) -> str:
        digest = hmac.new(self.config.secret_key.encode(), f"{label}:{pending_id}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class OIDCIdentity(IdentityProvider):
    _discovery: dict[str, Any] | None = None
    _discovered_at: float = 0

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None or time.time() - self._discovered_at > 3600:
            url = f"{self.config.oidc_issuer}/.well-known/openid-configuration"
            try:
                response = await self.http.get(url)
                response.raise_for_status()
            except httpx2.HTTPError as exc:
                raise LoginError(f"Can't reach the sign-in provider ({url}): {exc}") from exc
            self._discovery, self._discovered_at = response.json(), time.time()
        return self._discovery

    async def start(self, request: Request, pending_id: str) -> Response:
        meta = await self.discovery()
        verifier = self._derived("pkce", pending_id) + self._derived("pkce2", pending_id)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {
            "response_type": "code",
            "client_id": self.config.oidc_client_id,
            "redirect_uri": self.callback_url,
            "scope": self.config.oidc_scopes,
            "state": pending_id,
            "nonce": self._derived("nonce", pending_id),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return RedirectResponse(f"{meta['authorization_endpoint']}?{urlencode(params)}", status_code=302)

    async def finish(self, request: Request) -> tuple[str, Identity]:
        q = request.query_params
        pending_id = q.get("state", "")
        if q.get("error"):
            raise LoginError(f"Sign-in was cancelled or refused: {q.get('error_description') or q['error']}.")
        meta = await self.discovery()
        verifier = self._derived("pkce", pending_id) + self._derived("pkce2", pending_id)
        try:
            token = await self.http.post(meta["token_endpoint"], data={
                "grant_type": "authorization_code",
                "code": q.get("code", ""),
                "redirect_uri": self.callback_url,
                "client_id": self.config.oidc_client_id,
                "client_secret": self.config.oidc_client_secret,
                "code_verifier": verifier,
            }, headers={"Accept": "application/json"})
            token.raise_for_status()
            token_body = token.json()
            access = token_body["access_token"]
            info = await self.http.get(meta["userinfo_endpoint"], headers={"Authorization": f"Bearer {access}"})
            info.raise_for_status()
            claims = info.json()
            # Keycloak puts realm roles in the access token but not, by default, in userinfo. The tokens came
            # straight from the token endpoint over TLS (code flow), so their claims can fill gaps; userinfo wins.
            for jwt in (token_body.get("id_token"), access):
                for key, value in _jwt_claims(jwt).items():
                    claims.setdefault(key, value)
        except (httpx2.HTTPError, KeyError, ValueError) as exc:
            raise LoginError(f"The sign-in provider didn't complete the login: {exc}") from exc

        sub = claims.get("sub")
        if not sub:
            raise LoginError("The sign-in provider returned no user id (sub).")
        email = claims.get("email")
        self._check_allowed(claims, email)
        display = claims.get("preferred_username") or email or sub
        return pending_id, Identity(key=f"oidc:{self.config.oidc_issuer}#{sub}", display=display,
                                    email=email, name=claims.get("name"))

    def _check_allowed(self, claims: dict[str, Any], email: str | None) -> None:
        c = self.config
        if c.allow_any_user:
            return
        if c.oidc_required_role:
            value: Any = claims
            for part in c.oidc_role_claim.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            roles = value if isinstance(value, list) else ([value] if value else [])
            if c.oidc_required_role not in roles:
                raise LoginError(f"Your account doesn't have the {c.oidc_required_role!r} role needed for Inkcast.")
        if c.oidc_allowed_email_domains:
            domain = (email or "").rsplit("@", 1)[-1].lower()
            if not (email and claims.get("email_verified") and domain in c.oidc_allowed_email_domains):
                raise LoginError("Inkcast is limited to verified email addresses at: "
                                 + ", ".join(c.oidc_allowed_email_domains) + ".")


def _jwt_claims(token: str | None) -> dict[str, Any]:
    """Payload of a JWT without verifying it (only used for tokens received directly from the IdP)."""
    try:
        payload = (token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except (IndexError, ValueError):
        return {}


class GitHubIdentity(IdentityProvider):
    async def start(self, request: Request, pending_id: str) -> Response:
        scope = "read:user user:email" + (" read:org" if self.config.github_allowed_orgs else "")
        params = {"client_id": self.config.github_client_id, "redirect_uri": self.callback_url,
                  "state": pending_id, "scope": scope, "allow_signup": "false"}
        return RedirectResponse(f"{self.config.github_url}/login/oauth/authorize?{urlencode(params)}", status_code=302)

    async def finish(self, request: Request) -> tuple[str, Identity]:
        q = request.query_params
        pending_id = q.get("state", "")
        if q.get("error"):
            raise LoginError(f"GitHub sign-in was cancelled or refused: {q.get('error_description') or q['error']}.")
        api = self.config.github_api_url
        try:
            token = await self.http.post(f"{self.config.github_url}/login/oauth/access_token", data={
                "client_id": self.config.github_client_id, "client_secret": self.config.github_client_secret,
                "code": q.get("code", ""), "redirect_uri": self.callback_url,
            }, headers={"Accept": "application/json"})
            token.raise_for_status()
            access = token.json()["access_token"]
            headers = {"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"}
            user = (await self.http.get(f"{api}/user", headers=headers)).raise_for_status().json()
            orgs: list[str] = []
            if self.config.github_allowed_orgs:
                orgs = [o["login"].lower() for o in (await self.http.get(f"{api}/user/orgs", headers=headers)).raise_for_status().json()]
        except (httpx2.HTTPError, KeyError, ValueError) as exc:
            raise LoginError(f"GitHub didn't complete the login: {exc}") from exc

        login = user["login"].lower()
        c = self.config
        allowed = c.allow_any_user or login in c.github_allowed_users or bool(set(orgs) & set(c.github_allowed_orgs))
        if not allowed:
            raise LoginError(f"GitHub user @{user['login']} isn't allowed to use this Inkcast server.")
        return pending_id, Identity(key=f"github:{user['id']}", display=f"@{user['login']}",
                                    email=user.get("email"), name=user.get("name"))


class PasswordIdentity(IdentityProvider):
    async def start(self, request: Request, pending_id: str) -> Response:
        return pages.password_form(pending_id)

    async def finish(self, request: Request) -> tuple[str, Identity]:
        form = await request.form()
        pending_id = str(form.get("pending", ""))
        supplied = str(form.get("password", ""))
        if not hmac.compare_digest(supplied.encode(), self.config.password.encode()):
            await asyncio.sleep(1)  # slow down guessing
            raise LoginError("Wrong password.")
        return pending_id, Identity(key="local:owner", display="owner")


def make_identity_provider(config: Config, http: httpx2.AsyncClient) -> IdentityProvider:
    return {"oidc": OIDCIdentity, "github": GitHubIdentity, "password": PasswordIdentity}[config.login](config, http)

