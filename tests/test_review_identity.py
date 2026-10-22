"""Adversarial review of identity and tokens (services/identity, libs/auth, libs/db/identity.py): each test forces a
case that used to break, and fails without the fix committed with it."""

import asyncio
import base64
import hashlib
import re
import unittest
from urllib.parse import parse_qs, urljoin, urlparse

import httpx2

import test_e2e


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def form_fields(html: str) -> dict:
    return dict(re.findall(r'name=(\w+) value="([^"]*)"', html))


class ConsentBinding(test_e2e.Harness):
    """The consent page (and the token page) may only be answered by the browser that signed in. The flow's pending
    id travels in URLs (it is the OIDC ``state``), so whoever started a flow knows it: if that is enough to approve,
    an attacker who sends someone a sign-in link approves on their behalf the moment they arrive (with a live
    Keycloak session they never see a password prompt)."""

    VERIFIER = "attacker-verifier-" + "x" * 40

    async def _victim_reaches_consent(self, link: str) -> None:
        async with httpx2.AsyncClient(follow_redirects=True, timeout=20) as victim:   # follows, never clicks
            page = await victim.get(link)
        self.assertIn("/auth/consent", str(page.url))

    async def _attacker_approves(self, attacker: httpx2.AsyncClient, pending: str) -> httpx2.Response:
        base = self.config.public_url
        page = await attacker.get(f"{base}/auth/consent", params={"pending": pending})
        return await attacker.post(f"{base}/auth/consent", data={
            "pending": pending, "csrf": form_fields(page.text).get("csrf", ""), "decision": "approve"})

    def test_an_attacker_cannot_approve_the_oauth_consent_of_someone_who_followed_their_link(self):
        base, redirect = self.config.public_url, "https://attacker.example/cb"

        async def run():
            async with httpx2.AsyncClient(timeout=20) as attacker:
                client = (await attacker.post(f"{base}/register", json={
                    "client_name": "Claude Code", "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})).json()
                r = await attacker.get(f"{base}/authorize", params={
                    "client_id": client["client_id"], "response_type": "code", "redirect_uri": redirect, "state": "s",
                    "code_challenge": s256(self.VERIFIER), "code_challenge_method": "S256"})
                link = urljoin(base, r.headers["location"])                  # /auth/login?pending=...
                await self._victim_reaches_consent(link)
                r = await self._attacker_approves(attacker, parse_qs(urlparse(link).query)["pending"][0])
                code = parse_qs(urlparse(r.headers.get("location", "")).query).get("code", [None])[0]
                if not code:
                    return None
                token = (await attacker.post(f"{base}/token", data={
                    "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                    "client_id": client["client_id"], "code_verifier": self.VERIFIER})).json()
                me = await attacker.get(f"{base}/api/v1/me", headers={"Authorization": f"Bearer {token['access_token']}"})
                return me.status_code

        self.assertIsNone(asyncio.run(run()), "the attacker got an authorization code (and a token) for the victim")

    def test_an_attacker_cannot_approve_a_token_page_someone_else_signed_in_to(self):
        base = self.config.public_url

        async def run():
            async with httpx2.AsyncClient(timeout=20) as attacker:
                r = await attacker.get(f"{base}/tokens/new", params={"name": "ci", "scopes": "posts:write", "days": "366"})
                link = r.headers["location"]                                  # the IdP's URL, state = pending id
                await self._victim_reaches_consent(link)
                r = await self._attacker_approves(attacker, parse_qs(urlparse(link).query)["state"][0])
                return re.search(r"ink_pat_[A-Za-z0-9_\-]+", r.text)

        self.assertIsNone(asyncio.run(run()), "the attacker was shown a personal access token for the victim")

    def test_the_browser_that_signed_in_still_approves(self):
        browser = test_e2e.Browser()
        asyncio.run(browser(self._authorize_url()))
        self.assertTrue(browser.result.get("code"), browser.result)

    def _authorize_url(self) -> str:
        base = self.config.public_url
        client = httpx2.post(f"{base}/register", json={
            "client_name": "Claude Code", "redirect_uris": ["http://localhost:53682/callback"],
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"],
            "response_types": ["code"]}).json()
        return f"{base}/authorize?" + httpx2.QueryParams({
            "client_id": client["client_id"], "response_type": "code", "redirect_uri": "http://localhost:53682/callback",
            "state": "s", "code_challenge": s256("v" * 50), "code_challenge_method": "S256"}).__str__()


if __name__ == "__main__":
    unittest.main()
