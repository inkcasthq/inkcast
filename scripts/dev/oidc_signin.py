#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Sign in to Inkcast through a real Keycloak, the way a person does from Claude Code (E9.F9.1.S2, E9.F9.2.S1).

    KC_USER=... KC_PASSWORD=... [KC_EXPECT=allowed|refused|unverified] [KC_REGISTER=1 MAILPIT_URL=...] \\
        python oidc_signin.py [inkcast-url]

Plays the browser:
1. An MCP client registers and starts OAuth with PKCE at Inkcast, and Inkcast sends the browser to Keycloak.
2. The script answers Keycloak's pages as a person would. That means the login form, or with KC_REGISTER=1 the
   registration form, then the required actions: the verification email (read from Mailpit, its link followed)
   and the terms (accepted). There is no direct-grant shortcut; the realm disables it.
3. Keycloak sends the browser back to Inkcast's /auth/callback.
4. Inkcast asks for consent, and the client exchanges its code.

What counts as passing:
- KC_EXPECT=allowed (the default): the access token must reach /api/v1/me as that person.
- KC_EXPECT=refused: Inkcast must refuse the person (no inkcast-user role) and issue no code.
- KC_EXPECT=unverified: the person must be held at "verify your email" and never reach Inkcast.

With KC_SAVE_TOKENS=<file> (allowed only) it also makes a personal access token and writes both tokens to the file,
one per line, for a later check (the user-sync test: disabled in Keycloak, both must stop working).

Prints one line per step and exits non-zero on any surprise. Uses only what the Inkcast image has (Python and
httpx2), so CI runs it inside the pod.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
import sys
import time
from urllib.parse import parse_qs, urljoin, urlparse

import httpx2

REDIRECT = "http://localhost:53682/callback"


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def form(page: str, form_id: str | None = None) -> str | None:
    """The (unescaped) action of the form with `form_id`, or of the page's only POST form."""
    pattern = rf'<form[^>]*id="{form_id}"[^>]*action="([^"]+)"' if form_id else r'<form[^>]*action="([^"]+)"[^>]*method="post"'
    found = re.search(pattern, page, re.I) or (re.search(rf'<form[^>]*action="([^"]+)"[^>]*id="{form_id}"', page, re.I)
                                                if form_id else None)
    return html.unescape(found.group(1)) if found else None


def mailed_link(mailpit: str, to: str, since: float) -> str:
    """The action link in the newest email to `to` (Mailpit's API), waiting up to 60 s for it to arrive."""
    deadline = time.time() + 60
    while time.time() < deadline:
        found = httpx2.get(f"{mailpit}/api/v1/search", params={"query": f'to:"{to}"'}, timeout=10).json()
        for message in found.get("messages", []):
            body = httpx2.get(f"{mailpit}/api/v1/message/{message['ID']}", timeout=10).json()
            link = re.search(r'https?://[^\s"<>]+/login-actions/action-token\?[^\s"<>]+', body.get("Text") or body.get("HTML", ""))
            if link:
                return html.unescape(link.group(0))
        time.sleep(2)
    fail(f"no verification email to {to} arrived in Mailpit within 60 s")
    return ""


class Browser:
    def __init__(self, base: str):
        self.base, self.c = base, httpx2.Client(timeout=30, follow_redirects=False)

    def at_inkcast(self, r: httpx2.Response) -> bool:
        return r.is_redirect and (urljoin(str(r.url), r.headers["location"]).startswith(self.base)
                                  or r.headers["location"].startswith(REDIRECT))

    def follow(self, r: httpx2.Response) -> httpx2.Response:
        """Follow redirects that stay away from Inkcast (Keycloak's own hops)."""
        for _ in range(10):
            if not r.is_redirect or self.at_inkcast(r):
                return r
            r = self.c.get(urljoin(str(r.url), r.headers["location"]))
        return r


def keycloak(b: Browser, r: httpx2.Response, user: str, password: str, register: bool, mailpit: str,
             expect: str) -> httpx2.Response:
    """Answer Keycloak's pages until it sends the browser back to Inkcast. Returns that redirect."""
    r = b.follow(r)
    if register:
        link = re.search(r'href="([^"]*login-actions/registration[^"]*)"', r.text)
        if not link:
            fail(f"no registration link on Keycloak's login page ({r.url})")
        r = b.c.get(urljoin(str(r.url), html.unescape(link.group(1))))
        action = form(r.text, "kc-register-form")
        if not action:
            fail(f"no registration form at {r.url}: {r.text[:300]}")
        name = user.split("@")[0]
        r = b.follow(b.c.post(action, data={"email": user, "firstName": name.title(), "lastName": "Example",
                                            "password": password, "password-confirm": password}))
        print("2. Keycloak registration form submitted:", r.status_code)
    else:
        action = form(r.text, "kc-form-login")
        if not action:
            fail(f"no Keycloak login form at {r.url} (HTTP {r.status_code}): {r.text[:300]}")
        print("2. Keycloak login page:", urlparse(str(r.url)).netloc, urlparse(str(r.url)).path)
        r = b.follow(b.c.post(action, data={"username": user, "password": password, "credentialId": ""}))
        if not r.is_redirect and "kc-form-login" in r.text:
            fail("Keycloak did not sign the user in (the login form came back: wrong password?)")
        print("3. Keycloak accepted the password")

    for _ in range(8):                                   # required actions, in Keycloak's order
        if b.at_inkcast(r):
            return r
        page = r.text.lower()
        if "verify your email" in page or "email verification" in page or "execution=verify_email" in page:
            if expect == "unverified":
                print("4. held at 'verify your email': nothing reaches Inkcast before the address is verified")
                print("PASS: unverified held back")
                sys.exit(0)
            link = mailed_link(mailpit, user, time.time())
            print("4. verification email received; following its link")
            r = b.follow(b.c.get(link))
            continue
        if 'name="accept"' in page:
            action = form(r.text) or str(r.url)
            print("5. terms shown; accepting")
            r = b.follow(b.c.post(action, data={"accept": "Accept"}))
            continue
        if "your email address has been verified" in page or "proceed" in page:
            proceed = re.search(r'href="([^"]+)"[^>]*>\s*(?:&raquo;\s*)?(?:Click here to proceed|Back to Application)', r.text)
            if proceed:
                r = b.follow(b.c.get(html.unescape(proceed.group(1))))
                continue
        fail(f"an unexpected Keycloak page at {r.url} (HTTP {r.status_code}): {re.sub(r'<[^>]+>', ' ', r.text)[:400]}")
    fail("Keycloak never sent the browser back to Inkcast")
    return r


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8787").rstrip("/")
    user, password = os.environ["KC_USER"], os.environ["KC_PASSWORD"]
    expect = os.environ.get("KC_EXPECT", "allowed")
    register = os.environ.get("KC_REGISTER") == "1"
    mailpit = os.environ.get("MAILPIT_URL", "").rstrip("/")
    b = Browser(base)
    c = b.c
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
    # Inkcast's own hop to its sign-in start, then the redirect to Keycloak.
    for _ in range(5):
        if not (r.is_redirect and urljoin(str(r.url), r.headers["location"]).startswith(base)):
            break
        r = c.get(urljoin(str(r.url), r.headers["location"]))

    r = keycloak(b, r, user, password, register, mailpit, expect)
    if expect == "unverified":
        fail("an unverified person got past Keycloak to Inkcast")

    # Keycloak -> Inkcast /auth/callback -> consent (or a refusal page).
    for _ in range(10):
        if not r.is_redirect or "/auth/consent" in r.headers["location"] or r.headers["location"].startswith(REDIRECT):
            break
        r = c.get(urljoin(str(r.url), r.headers["location"]))
    if expect == "refused":
        if r.is_redirect and ("/auth/consent" in r.headers["location"] or r.headers["location"].startswith(REDIRECT)):
            fail("Inkcast let a person without the inkcast-user role through to consent")
        if "role" not in r.text:
            fail(f"expected a refusal naming the missing role, got HTTP {r.status_code}: {r.text[:300]}")
        print("6. Inkcast refused the sign-in:", r.status_code, "(the account lacks inkcast-user)")
        print("PASS: refused as expected")
        return
    if not (r.is_redirect and "/auth/consent" in r.headers["location"]):
        fail(f"Inkcast did not reach consent (HTTP {r.status_code}): {r.text[:300]}")
    consent_url = urljoin(str(r.url), r.headers["location"])
    page = c.get(consent_url)
    fields = dict(re.findall(r'name=(\w+) value="([^"]*)"', page.text))
    print("6. Inkcast consent page for:", re.search(r"Signed in as <strong>([^<]+)</strong>", page.text).group(1))
    r = c.post(urljoin(consent_url, "/auth/consent"),
               data={"pending": fields["pending"], "csrf": fields["csrf"], "decision": "approve"})
    query = parse_qs(urlparse(r.headers.get("location", "")).query)
    if query.get("state", [""])[0] != state or "code" not in query:
        fail(f"no authorization code after consent: {r.headers.get('location')}")
    token = c.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
        "client_id": client["client_id"], "code_verifier": verifier, "resource": f"{base}/mcp"}).raise_for_status().json()
    print("7. Token issued, scope:", token.get("scope"))
    me = c.get(f"{base}/api/v1/me", headers={"Authorization": f"Bearer {token['access_token']}"}).raise_for_status().json()
    if user.split("@")[0] not in " ".join(str(v) for v in me.values()):
        fail(f"/api/v1/me doesn't name {user}: {me}")
    print("8. /api/v1/me:", {k: me[k] for k in me if k in ("display", "email", "name", "id")})
    if os.environ.get("KC_SAVE_TOKENS"):
        pat = c.post(f"{base}/api/v1/tokens", headers={"Authorization": f"Bearer {token['access_token']}"},
                     json={"name": "user-sync check", "scopes": ["posts:read"]}).raise_for_status().json()["token"]
        with open(os.environ["KC_SAVE_TOKENS"], "w", encoding="utf-8") as out:
            out.write(f"{token['access_token']}\n{pat}\n")
        print("9. OAuth token and a personal access token saved for the user-sync check")
    print("PASS: signed in through Keycloak" + (" after signing up" if register else ""))


if __name__ == "__main__":
    main()
