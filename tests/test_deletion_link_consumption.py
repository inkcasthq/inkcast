# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A deletion link (/account/delete/<ticket>) is used up only once the sign-in redirect is ready (issue #149), and
only by the POST from its page (issue #401).

Opening the link (GET) shows a page and changes nothing. Continue, the POST, reads the request behind the link, starts
the sign-in (with Keycloak: discovery, then a redirect to its authorization endpoint) and only then consumes the link.
A sign-in provider that is down leaves the link good for another try, instead of "Can't sign in" on a link that can
never be opened again. The consume is one transaction, so of two tabs that press Continue on the same link only the
first to have its redirect ready proceeds; the other sees "Link expired", and so does every later visit."""

import asyncio
import html
import re
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx2
from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http.app import compose
from inkcast.services.identity import DELETE_REQUEST
from inkcast.services.identity.login import OIDCIdentity

PUBLIC_URL = "http://localhost:8787"
ISSUER = "https://sso.example.com/realms/inkcast"
AUTHORIZE = f"{ISSUER}/protocol/openid-connect/auth"
DISCOVERY = {"authorization_endpoint": AUTHORIZE, "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
             "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo"}


class Provider:
    """Keycloak as the identity service sees it: its discovery document, served or refused (connection refused)."""

    def __init__(self, up: bool) -> None:
        self.up = up
        self.requests = 0

    def transport(self) -> httpx2.MockTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests += 1
            if not self.up:
                raise httpx2.ConnectError("connection refused", request=request)
            return httpx2.Response(200, json=DISCOVERY)
        return httpx2.MockTransport(handle)


def keycloak_app(provider: Provider, **overrides):
    """(ASGI app, context) for the identity service signing in through ``provider``."""
    cfg = Config(**{"public_url": PUBLIC_URL, "secret_key": "k" * 40, "database_path": ":memory:", "login": "oidc",
                    "oidc_issuer": ISSUER, "oidc_client_id": "inkcast", "oidc_client_secret": "kc-secret",
                    "keycloak_sync_client_id": "inkcast-sync", "keycloak_sync_secret": "sync-secret", **overrides})
    specs = resolve("IdentityService")
    ctx = build(cfg, specs, transport=provider.transport())
    return compose(specs, ctx), ctx


def a_link(ctx) -> str:
    user = ctx.store.upsert_user(f"oidc:{ISSUER}#owner", "owner", None, None)
    return ctx.store.create_deletion_request(user["id"], ttl=900)


def pending_rows(ctx) -> list[dict]:
    return [dict(r) for r in ctx.store._all("SELECT * FROM pending_auth WHERE client_id = ?", DELETE_REQUEST)]


def link_hash(ctx):
    return ctx.store._one("SELECT link_hash FROM deletion_requests")["link_hash"]


def csrf_of(page: str) -> str:
    """The token the link's page binds its Continue to (issue #401)."""
    return re.search(r'name=csrf value="([^"]*)"', page).group(1)


def press_continue(c: TestClient, ticket: str):
    """Open the link's page, then press Continue: the POST that starts the sign-in (issue #401)."""
    page = c.get(f"/account/delete/{ticket}")
    assert page.status_code == 200, page.text[:300]
    return c.post(f"/account/delete/{ticket}", data={"csrf": csrf_of(page.text)})


class ConsumedOnceTheRedirectIsReady(unittest.TestCase):
    def test_a_link_opened_while_the_provider_is_unreachable_can_be_opened_again(self):
        provider = Provider(up=False)
        app, ctx = keycloak_app(provider)
        ticket = a_link(ctx)
        with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            down = press_continue(c, ticket)
            self.assertEqual(down.status_code, 502, down.text)
            self.assertIn(html.escape("Can't sign in"), down.text)
            self.assertEqual(provider.requests, 1, "discovery was tried")
            self.assertIsNotNone(link_hash(ctx), "the link is not used up: the person never got to the sign-in")
            self.assertEqual(pending_rows(ctx), [], "no sign-in is left pending either")

            provider.up = True
            back = press_continue(c, ticket)
            self.assertEqual(back.status_code, 302, back.text)
            self.assertTrue(back.headers["location"].startswith(AUTHORIZE + "?"), back.headers["location"])
            query = parse_qs(urlparse(back.headers["location"]).query)
            self.assertNotIn("kc_action", query, "a sign-in, not yet the delete_account action")
            (pending,) = pending_rows(ctx)
            self.assertEqual(query["state"], [pending["id"]], "the redirect's state is the pending sign-in")
            self.assertEqual(pending["params"], ctx.store._one("SELECT id FROM deletion_requests")["id"])
            self.assertIsNone(link_hash(ctx), "used up now, with the redirect ready")

            again = c.get(f"/account/delete/{ticket}")
            self.assertEqual(again.status_code, 400, again.text)
            self.assertIn("Link expired", again.text)
            self.assertEqual(len(pending_rows(ctx)), 1, "the used link starts nothing")

    def test_two_tabs_that_open_the_same_link_get_one_redirect(self):
        """Both tabs press Continue and have their redirect ready before either consumes the link: the consume
        decides, once."""
        provider = Provider(up=True)
        app, ctx = keycloak_app(provider)
        ticket = a_link(ctx)
        barrier = asyncio.Barrier(2)
        real_start = OIDCIdentity.start
        met: list[bool] = []

        async def start_then_wait(self, request, pending_id):
            response = await real_start(self, request, pending_id)
            try:                                         # neither tab goes on until both redirects are ready
                await asyncio.wait_for(barrier.wait(), timeout=5)
                met.append(True)
            except TimeoutError:                         # the other tab never got this far: it was refused before
                met.append(False)
            return response

        async def two_tabs():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC_URL) as c:
                csrf = csrf_of((await c.get(f"/account/delete/{ticket}")).text)
                with mock.patch.object(OIDCIdentity, "start", start_then_wait):
                    first, second = await asyncio.gather(c.post(f"/account/delete/{ticket}", data={"csrf": csrf}),
                                                         c.post(f"/account/delete/{ticket}", data={"csrf": csrf}))
                third = await c.post(f"/account/delete/{ticket}", data={"csrf": csrf})
            return first, second, third

        first, second, third = asyncio.run(two_tabs())
        self.assertEqual(met, [True, True], "both tabs had their redirect ready before either consumed the link")
        self.assertEqual(sorted((first.status_code, second.status_code)), [302, 400], (first.text, second.text))
        winner = first if first.status_code == 302 else second
        loser = second if winner is first else first
        self.assertIn("Link expired", loser.text)
        (pending,) = pending_rows(ctx)
        self.assertEqual(parse_qs(urlparse(winner.headers["location"]).query)["state"], [pending["id"]],
                         "only the tab that got the redirect has a sign-in pending; the other's was dropped")
        self.assertIsNone(link_hash(ctx))
        self.assertEqual(third.status_code, 400, "and the link is used up for everyone after")

    def test_the_password_form_uses_the_link_up_as_the_redirect_does(self):
        """With the shared-password sign-in there is no provider to reach: the form is the sign-in, so Continue showing
        it uses the link up, as the redirect does. Opening the link shows only the page, as with Keycloak."""
        cfg = Config(public_url=PUBLIC_URL, secret_key="k" * 40, database_path=":memory:", login="password", password="p" * 12)
        specs = resolve("IdentityService")
        ctx = build(cfg, specs)
        ticket = a_link(ctx)
        with TestClient(compose(specs, ctx), base_url=PUBLIC_URL, follow_redirects=False) as c:
            start = c.get(f"/account/delete/{ticket}")
            self.assertEqual(start.status_code, 200, start.text)
            self.assertIn("Nothing is deleted yet", start.text)
            self.assertNotIn("password", start.text, "opening the link shows the page, not yet the sign-in")
            self.assertIsNotNone(link_hash(ctx))
            form = press_continue(c, ticket)
            self.assertEqual(form.status_code, 200, form.text)
            self.assertIn("password", form.text)
            self.assertEqual(len(pending_rows(ctx)), 1)
            self.assertIsNone(link_hash(ctx))
            self.assertEqual(c.get(f"/account/delete/{ticket}").status_code, 400)


if __name__ == "__main__":
    unittest.main()
