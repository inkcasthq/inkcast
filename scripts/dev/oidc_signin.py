#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Sign in to Inkcast through a real Keycloak, the way a person does from Claude Code (E9.F9.1.S2).

    KC_USER=... KC_PASSWORD=... [KC_EXPECT=allowed|refused] python oidc_signin.py [inkcast-url]

Plays the browser: an MCP client registers and starts OAuth with PKCE at Inkcast; Inkcast sends the browser to
Keycloak; the script fills Keycloak's own login form (no direct-grant shortcut, which the realm disables); Keycloak
sends the browser back to Inkcast's /auth/callback; Inkcast asks for consent; the client exchanges its code. With
KC_EXPECT=allowed (default) the access token must reach /api/v1/me as that person; with refused, Inkcast must
refuse the sign-in (the account lacks the inkcast-user role) and issue no code. Prints one line per step; exits
non-zero on any surprise. Uses only what the Inkcast image has (Python and httpx2), so CI runs it in the pod.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
import sys
from urllib.parse import parse_qs, urljoin, urlparse

import httpx2

REDIRECT = "http://localhost:53682/callback"


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8787").rstrip("/")
    user, password = os.environ["KC_USER"], os.environ["KC_PASSWORD"]
    expect = os.environ.get("KC_EXPECT", "allowed")
    with httpx2.Client(timeout=30, follow_redirects=False) as c:
        meta = c.get(f"{base}/.well-known/oauth-authorization-server").raise_for_status().json()
        client = c.post(meta["registration_endpoint"], json={
            "client_name": "Claude Code", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).raise_for_status().json()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(12)
        r = c.get(meta["authorization_endpoint"], params={
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256", "resource": f"{base}/mcp",
            "scope": " ".join(meta.get("scopes_supported") or [])})
        print("1. Inkcast /authorize:", r.status_code)

        # Follow redirects until Keycloak shows its login form.
        hops = 0
        while r.is_redirect and hops < 10:
            r, hops = c.get(urljoin(str(r.url), r.headers["location"])), hops + 1
        form = re.search(r'<form[^>]+id="kc-form-login"[^>]*action="([^"]+)"', r.text)
        if not form:
            fail(f"no Keycloak login form at {r.url} (HTTP {r.status_code}): {r.text[:300]}")
        print("2. Keycloak login page:", urlparse(str(r.url)).netloc, urlparse(str(r.url)).path)
        r = c.post(html.unescape(form.group(1)), data={"username": user, "password": password, "credentialId": ""})
        print("3. Keycloak accepted the password:", r.status_code)
        if not r.is_redirect:
            fail(f"Keycloak did not sign the user in (HTTP {r.status_code}): {r.text[:300]}")

        # Keycloak -> Inkcast /auth/callback -> consent (or a refusal page).
        hops = 0
        while r.is_redirect and "/auth/consent" not in r.headers["location"] and hops < 10:
            location = urljoin(str(r.url), r.headers["location"])
            if location.startswith(REDIRECT):
                break
            r, hops = c.get(location), hops + 1
        if expect == "refused":
            if r.is_redirect and ("/auth/consent" in r.headers["location"] or r.headers["location"].startswith(REDIRECT)):
                fail("Inkcast let a user without the inkcast-user role through to consent")
            if "role" not in r.text:
                fail(f"expected a refusal naming the missing role, got HTTP {r.status_code}: {r.text[:300]}")
            print("4. Inkcast refused the sign-in:", r.status_code, "(the account lacks inkcast-user)")
            print("PASS: refused as expected")
            return
        if not (r.is_redirect and "/auth/consent" in r.headers["location"]):
            fail(f"Inkcast did not reach consent (HTTP {r.status_code}): {r.text[:300]}")
        consent_url = urljoin(str(r.url), r.headers["location"])
        page = c.get(consent_url)
        fields = dict(re.findall(r'name=(\w+) value="([^"]*)"', page.text))
        print("4. Inkcast consent page for:", re.search(r"Signed in as <strong>([^<]+)</strong>", page.text).group(1))
        r = c.post(urljoin(consent_url, "/auth/consent"),
                   data={"pending": fields["pending"], "csrf": fields["csrf"], "decision": "approve"})
        query = parse_qs(urlparse(r.headers.get("location", "")).query)
        if query.get("state", [""])[0] != state or "code" not in query:
            fail(f"no authorization code after consent: {r.headers.get('location')}")
        token = c.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
            "client_id": client["client_id"], "code_verifier": verifier, "resource": f"{base}/mcp"}).raise_for_status().json()
        print("5. Token issued, scope:", token.get("scope"))
        me = c.get(f"{base}/api/v1/me", headers={"Authorization": f"Bearer {token['access_token']}"}).raise_for_status().json()
        shown = " ".join(str(v) for v in me.values())
        if user.split("@")[0] not in shown:
            fail(f"/api/v1/me doesn't name {user}: {me}")
        print("6. /api/v1/me:", {k: me[k] for k in me if k in ("display", "email", "name", "id")})
        print("PASS: signed in through Keycloak")


if __name__ == "__main__":
    main()
