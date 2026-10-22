#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Delete an Inkcast account through a real Keycloak, the way a person does (E9.F9.5.S4, issue #41).

    KC_USER=... KC_PASSWORD=... KC_TOKENS=<file from oidc_signin.py --save> python account_delete.py [inkcast-url]
    KC_USER=... KC_PASSWORD=... KC_MODE=console python account_delete.py [inkcast-url]

Plays the browser:
1. ``POST /api/v1/account/deletion-link`` with the person's OAuth token (first line of KC_TOKENS): a one-time link.
2. Opens it: Inkcast sends the browser to Keycloak for a fresh sign-in, then shows its own page (what goes, what stays).
3. Continue: Inkcast sends the browser to Keycloak's ``delete_account`` application-initiated action; Keycloak asks
   for the password once more (the action always re-authenticates) and shows its confirmation page.
4. Confirm: Keycloak deletes the account and shows its "deleted" page, whose "Back to Application" link (the
   client's baseUrl) is Inkcast's ``/account/return``.
5. Follows it: Inkcast confirms the person is gone through the user sync's read-only view-users, erases, and says so.

What counts as passing: Inkcast's page says the account is deleted; both saved tokens answer 401; the metric
``inkcast_identity_users_deleted_total`` counts exactly one erasure, ``via="return"`` (this visit) or ``via="event"``
(the sync read her DELETE_ACCOUNT event first; the page still said deleted). The caller checks Keycloak and the database.

``KC_MODE=console`` instead deletes from Keycloak's side only, as the account console does it: the account
console's own ``kc_action=delete_account`` for the ``account-console`` client, with no Inkcast page involved
(the console's React UI is not driven; its request to Keycloak is). Inkcast must then erase the person from the
``DELETE_ACCOUNT`` user event; the caller waits for that.

Uses only what the Inkcast image has (Python and httpx2), so CI runs it inside the pod.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
import sys
from urllib.parse import urljoin, urlparse

import httpx2


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def form_action(page: str, form_id: str | None = None) -> str | None:
    pattern = rf'<form[^>]*id="{form_id}"[^>]*action="([^"]+)"' if form_id else r'<form[^>]*action="([^"]+)"[^>]*method="post"'
    found = re.search(pattern, page, re.I) or (re.search(rf'<form[^>]*action="([^"]+)"[^>]*id="{form_id}"', page, re.I)
                                                if form_id else None)
    return html.unescape(found.group(1)) if found else None


class Browser:
    def __init__(self, base: str):
        self.base, self.c = base, httpx2.Client(timeout=30, follow_redirects=False)

    def at_inkcast(self, r: httpx2.Response) -> bool:
        return r.is_redirect and urljoin(str(r.url), r.headers["location"]).startswith(self.base)

    def follow(self, r: httpx2.Response, stop_at_inkcast: bool = True) -> httpx2.Response:
        for _ in range(10):
            if not r.is_redirect or (stop_at_inkcast and self.at_inkcast(r)):
                return r
            r = self.c.get(urljoin(str(r.url), r.headers["location"]))
        return r

    def sign_in_at_keycloak(self, r: httpx2.Response, user: str, password: str) -> httpx2.Response:
        """Answer Keycloak's login form; the response is whatever Keycloak shows next."""
        r = self.follow(r)
        action = form_action(r.text, "kc-form-login")
        if not action:
            fail(f"no Keycloak login form at {r.url} (HTTP {r.status_code}): {re.sub(r'<[^>]+>', ' ', r.text)[:300]}")
        r = self.follow(self.c.post(action, data={"username": user, "password": password, "credentialId": ""}))
        if not r.is_redirect and "kc-form-login" in r.text:
            fail("Keycloak did not sign the user in (wrong password?)")
        return r

    def confirm_delete(self, r: httpx2.Response) -> httpx2.Response:
        """Keycloak's delete-account confirmation page: press Delete Account."""
        if "delete-account" not in r.text.lower() and "delete account" not in r.text.lower():
            fail(f"expected Keycloak's delete-account confirmation at {r.url} (HTTP {r.status_code}): "
                 f"{re.sub(r'<[^>]+>', ' ', r.text)[:300]}")
        action = form_action(r.text) or str(r.url)
        return self.c.post(action, data={})


def through_inkcast(b: Browser, base: str, user: str, password: str, tokens: list[str]) -> None:
    c = b.c
    link = c.post(f"{base}/api/v1/account/deletion-link", headers={"Authorization": f"Bearer {tokens[0]}"})
    if link.status_code != 200:
        fail(f"deletion-link answered HTTP {link.status_code}: {link.text[:300]}")
    url = link.json()["url"]
    print("1. one-time link from POST /api/v1/account/deletion-link, expires in", link.json()["expires_in"], "s")

    r = c.get(url)
    while r.is_redirect and urljoin(str(r.url), r.headers["location"]).startswith(base):
        r = c.get(urljoin(str(r.url), r.headers["location"]))
    r = b.sign_in_at_keycloak(r, user, password)                      # a fresh sign-in, as /tokens/new asks
    print("2. signed in again at Keycloak for the deletion page")
    for _ in range(10):
        if not r.is_redirect or "/auth/consent" in r.headers["location"]:
            break
        r = c.get(urljoin(str(r.url), r.headers["location"]))
    if not (r.is_redirect and "/auth/consent" in r.headers["location"]):
        fail(f"Inkcast did not show its deletion page (HTTP {r.status_code}): {r.text[:300]}")
    page = c.get(urljoin(str(r.url), r.headers["location"]))
    for said in ("Delete your Inkcast account?", "What goes", "What stays", "articles already published"):
        if said not in page.text:
            fail(f"Inkcast's page does not say {said!r}")
    print("3. Inkcast's page: what goes, what stays; Continue")
    fields = dict(re.findall(r'name=(\w+) value="([^"]*)"', page.text))
    r = c.post(f"{base}/auth/consent", data={"pending": fields["pending"], "csrf": fields["csrf"], "decision": "approve"})
    if not (r.is_redirect and "kc_action=delete_account" in r.headers["location"]):
        fail(f"Continue did not send the browser to Keycloak's delete_account action: HTTP {r.status_code} {r.headers.get('location')}")
    if "inkcast_deletion=" not in r.headers.get("set-cookie", ""):
        fail("no return cookie set with the redirect to Keycloak")
    r = c.get(r.headers["location"])
    r = b.sign_in_at_keycloak(r, user, password)                      # delete_account always re-authenticates
    print("4. Keycloak asked for the password again; its confirmation page is up")
    r = b.confirm_delete(r)
    if r.is_redirect:
        fail(f"Keycloak redirected instead of deleting: {r.headers['location']}")
    if "deleted" not in r.text.lower():
        fail(f"Keycloak's page after confirming does not say deleted: {re.sub(r'<[^>]+>', ' ', r.text)[:300]}")
    back = re.search(r'href="([^"]+)"[^>]*>[^<]*Back to Application', r.text, re.I)
    if not back:
        fail(f"Keycloak's deleted page has no Back to Application link (client baseUrl): {re.sub(r'<[^>]+>', ' ', r.text)[:300]}")
    back_url = html.unescape(back.group(1))
    print("5. Keycloak deleted the account; following its Back to Application link:", urlparse(back_url).path)
    home = b.follow(c.get(back_url), stop_at_inkcast=False)
    if home.status_code != 200 or "Your account is deleted" not in home.text:
        fail(f"Inkcast's return page did not confirm the deletion (HTTP {home.status_code}): "
             f"{re.sub(r'<[^>]+>', ' ', home.text)[:300]}")
    print("6. Inkcast: account deleted, data erased (via=return)")
    codes = [httpx2.get(f"{base}/api/v1/me", headers={"Authorization": f"Bearer {t}"}).status_code for t in tokens]
    if codes != [401] * len(tokens):
        fail(f"the saved tokens answer {codes}, expected all 401")
    print("7. both saved tokens answer 401")
    metrics = httpx2.get(os.environ.get("METRICS_URL", "http://localhost:9090/metrics")).text
    line = [ln for ln in metrics.splitlines() if ln.startswith("inkcast_identity_users_deleted_total{")]
    if len(line) != 1 or not line[0].endswith(" 1") or not ('via="return"' in line[0] or 'via="event"' in line[0]):
        fail(f"expected exactly one erasure counted (via=return or via=event), got {line}")
    print("   ", *line)
    print("PASS: deleted through the Inkcast flow on the real Keycloak")


def through_account_console(b: Browser, user: str, password: str, issuer: str) -> None:
    """The account console's own request: kc_action=delete_account for the account-console client."""
    c = b.c
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get(f"{issuer}/protocol/openid-connect/auth", params={
        "client_id": "account-console", "redirect_uri": f"{issuer}/account/", "response_type": "code",
        "scope": "openid", "state": secrets.token_urlsafe(12), "kc_action": "delete_account",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    r = b.sign_in_at_keycloak(r, user, password)
    print("1. signed in at Keycloak as the account console does, with kc_action=delete_account")
    r = b.confirm_delete(r)
    if r.is_redirect or "deleted" not in r.text.lower():
        fail(f"Keycloak did not delete the account: HTTP {r.status_code} {r.headers.get('location', '')} "
             f"{re.sub(r'<[^>]+>', ' ', r.text)[:300]}")
    print("2. Keycloak deleted the account (no Inkcast page involved: the DELETE_ACCOUNT event must do the rest)")
    print("PASS: deleted from Keycloak's side")


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8787").rstrip("/")
    user, password = os.environ["KC_USER"], os.environ["KC_PASSWORD"]
    b = Browser(base)
    if os.environ.get("KC_MODE") == "console":
        through_account_console(b, user, password, os.environ["KC_ISSUER"].rstrip("/"))
        return
    with open(os.environ["KC_TOKENS"], encoding="utf-8") as f:
        tokens = f.read().split()
    through_inkcast(b, base, user, password, tokens)


if __name__ == "__main__":
    main()
