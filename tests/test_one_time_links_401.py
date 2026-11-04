# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A one-time link is used up by the POST from its page, never by a GET or a HEAD (issue #401).

A GET on /account/delete/<ticket> used the link up (once the sign-in redirect was ready, #149), and Starlette answers
HEAD on every GET route: a chat unfurl, a mail scanner, an assistant fetching the URL it was handed, spent the link
before the person opened it. Now a GET or a HEAD on either link shows a page and changes nothing (RFC 9110 §9.2.1:
safe methods are what prefetchers send); the POST from the page's button starts the sign-in, or saves the key, and
uses the link up once, in the same single-use statement as before.

The POST carries a token bound to the ticket (an HMAC under a key derived from INKCAST_SECRET_KEY, libs/http/onetime):
only a form Inkcast served for that ticket can answer, so a blind POST, a form on another site or a token from
another link is refused before anything is started, and SameSite cookies are not the only defence (there is no
cookie at all on these pages: the GET writes nothing and sets nothing)."""

import asyncio
import re
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx2
from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http.app import compose
from inkcast.libs.platforms.devto import DevtoPlatform
from test_connect_tickets_399 import GOOD_KEY, Dev, a_ticket
from test_deletion_link_consumption import AUTHORIZE, PUBLIC_URL, Provider, a_link, keycloak_app, link_hash, pending_rows


def csrf_of(page: str) -> str:
    return dict(re.findall(r'name=(\w+) value="([^"]*)"', page))["csrf"]


class DeletionLinkGetAndHead(unittest.TestCase):
    def test_get_and_head_show_the_page_and_leave_the_link_good(self):
        provider = Provider(up=True)
        app, ctx = keycloak_app(provider)
        self.addCleanup(ctx.store.close)
        ticket = a_link(ctx)
        with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            for _ in range(3):                                     # a previewer, a scanner, then the person
                page = c.get(f"/account/delete/{ticket}")
                self.assertEqual(page.status_code, 200, page.text[:300])
                head = c.head(f"/account/delete/{ticket}")
                self.assertEqual(head.status_code, 200, head.text[:300])
                self.assertEqual(head.content, b"")
            self.assertIsNotNone(link_hash(ctx), "no GET or HEAD used the link up")
            self.assertEqual(pending_rows(ctx), [], "and none started a sign-in")
            self.assertEqual(provider.requests, 0, "the provider was not even asked")
            self.assertIn("Nothing is deleted yet", page.text)
            self.assertRegex(page.text, rf'<form method=post action="/account/delete/{ticket}">')
            self.assertIn("<button class=primary type=submit>Continue</button>", page.text)
            self.assertIn("name=csrf value=", page.text)
            self.assertNotIn("<script", page.text, "the button needs no script")
            self.assertNotIn("set-cookie", page.headers, "the page sets no cookie")
            self.assertEqual(page.headers["cache-control"], "no-store")

    def test_an_unknown_or_used_link_is_refused_on_get_and_head_alike(self):
        app, ctx = keycloak_app(Provider(up=True))
        self.addCleanup(ctx.store.close)
        with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            self.assertEqual(c.get("/account/delete/tkt_never_issued").status_code, 400)
            self.assertEqual(c.head("/account/delete/tkt_never_issued").status_code, 400)


class DeletionLinkPost(unittest.TestCase):
    def test_the_post_from_the_page_starts_the_sign_in_and_uses_the_link_up_once(self):
        provider = Provider(up=True)
        app, ctx = keycloak_app(provider)
        self.addCleanup(ctx.store.close)
        ticket = a_link(ctx)
        with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            csrf = csrf_of(c.get(f"/account/delete/{ticket}").text)
            r = c.post(f"/account/delete/{ticket}", data={"csrf": csrf})
            self.assertEqual(r.status_code, 302, r.text[:300])
            self.assertTrue(r.headers["location"].startswith(AUTHORIZE + "?"), r.headers["location"])
            (pending,) = pending_rows(ctx)
            self.assertEqual(parse_qs(urlparse(r.headers["location"]).query)["state"], [pending["id"]])
            self.assertIsNone(link_hash(ctx), "used up by the POST")
            self.assertEqual(provider.requests, 1, "discovery once, for this sign-in")
            again = c.post(f"/account/delete/{ticket}", data={"csrf": csrf})
            self.assertEqual(again.status_code, 400, "a second POST with the same page's token finds nothing")
            self.assertIn("Link expired", again.text)
            self.assertEqual(c.get(f"/account/delete/{ticket}").status_code, 400)
            self.assertEqual(len(pending_rows(ctx)), 1, "the used link starts nothing more")

    def test_a_post_without_the_pages_token_is_refused_and_changes_nothing(self):
        provider = Provider(up=True)
        app, ctx = keycloak_app(provider)
        self.addCleanup(ctx.store.close)
        ticket, other = a_link(ctx), ctx.store.create_deletion_request(ctx.store.upsert_user("oidc:x#2", "two", None, None)["id"], ttl=900)
        with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            others = csrf_of(c.get(f"/account/delete/{other}").text)
            for data in ({}, {"csrf": ""}, {"csrf": "not-the-token"}, {"csrf": others}):
                r = c.post(f"/account/delete/{ticket}", data=data)
                self.assertEqual(r.status_code, 400, (data, r.text[:300]))
                self.assertIn("Request rejected", r.text)
            self.assertEqual([r["link_hash"] is not None for r in ctx.store._all("SELECT link_hash FROM deletion_requests")],
                             [True, True], "both links are still good")
            self.assertEqual(pending_rows(ctx), [])
            self.assertEqual(provider.requests, 0, "refused before the provider is asked")
            self.assertEqual(c.post("/account/delete/tkt_never_issued", data={"csrf": others}).status_code, 400)
            good = c.post(f"/account/delete/{ticket}", data={"csrf": csrf_of(c.get(f"/account/delete/{ticket}").text)})
            self.assertEqual(good.status_code, 302, "the page's own token still works after the refused tries")

    def test_the_posts_are_rate_limited_per_client_and_so_are_the_gets(self):
        app, ctx = keycloak_app(Provider(up=True), rate_limit_per_minute=2)
        self.addCleanup(ctx.store.close)
        with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            self.assertEqual([c.post("/account/delete/nope", data={}).status_code for _ in range(3)], [400, 400, 429])
        app, ctx = keycloak_app(Provider(up=True), rate_limit_per_minute=2)
        self.addCleanup(ctx.store.close)
        with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            self.assertEqual([c.get("/account/delete/nope").status_code for _ in range(3)], [400, 400, 429])


class ConnectLink(unittest.TestCase):
    def connector_app(self, dev: Dev):
        cfg = Config(public_url=PUBLIC_URL, secret_key="k" * 40, database_path=":memory:", login="password", password="p" * 12)
        specs = resolve("ConnectorService")
        ctx = build(cfg, specs, transport=httpx2.MockTransport(dev))
        self.addCleanup(ctx.store.close)
        return compose(specs, ctx), ctx

    def test_get_and_head_show_the_form_and_ask_the_platform_nothing(self):
        dev = Dev()
        app, ctx = self.connector_app(dev)
        user, ticket = a_ticket(ctx)
        with TestClient(app, base_url=PUBLIC_URL) as c, \
                mock.patch.object(DevtoPlatform, "verify", wraps=DevtoPlatform.verify, autospec=True) as verify:
            for _ in range(2):
                page = c.get(f"/connect/{ticket}")
                self.assertEqual(page.status_code, 200)
                head = c.head(f"/connect/{ticket}")
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b"")
            self.assertEqual(verify.call_count, 0, "a HEAD takes the GET's path: nothing is verified")
            self.assertEqual(dev.verified, 0)
            self.assertIsNotNone(ctx.store.get_ticket(ticket), "the link is still good")
            self.assertEqual(ctx.store.connected_accounts(user), {})
            self.assertRegex(page.text, rf'<form method=post action="/connect/{ticket}">')
            self.assertIn("name=csrf value=", page.text)
            self.assertNotIn("set-cookie", page.headers)

    def test_a_post_without_the_forms_token_is_refused_before_the_platform_is_asked(self):
        dev = Dev()
        app, ctx = self.connector_app(dev)
        user, ticket = a_ticket(ctx)
        with TestClient(app, base_url=PUBLIC_URL) as c:
            for data in ({"api_key": GOOD_KEY}, {"api_key": GOOD_KEY, "csrf": "not-the-token"}):
                r = c.post(f"/connect/{ticket}", data=data)
                self.assertEqual(r.status_code, 400, (data, r.text[:300]))
                self.assertIn("Request rejected", r.text)
            self.assertEqual(dev.verified, 0, "DEV was not asked")
            self.assertIsNotNone(ctx.store.get_ticket(ticket))
            self.assertEqual(ctx.store.connected_accounts(user), {})
            csrf = csrf_of(c.get(f"/connect/{ticket}").text)
            wrong = c.post(f"/connect/{ticket}", data={"api_key": "wrong", "csrf": csrf})
            self.assertEqual(wrong.status_code, 200, "the form again, with DEV's reason, carrying the token again")
            self.assertEqual(csrf_of(wrong.text), csrf)
            done = c.post(f"/connect/{ticket}", data={"api_key": GOOD_KEY, "csrf": csrf})
            self.assertEqual(done.status_code, 200, done.text[:300])
            self.assertIn("DEV connected", done.text)
            self.assertIsNone(ctx.store.get_ticket(ticket), "used up by the POST that connected")
            self.assertEqual(c.get(f"/connect/{ticket}").status_code, 400)
        self.assertEqual(dev.verified, 2)


class TheTokenIsBoundToTheTicket(unittest.TestCase):
    def test_make_and_check(self):
        from inkcast.libs.http.onetime import PageTokens
        tokens, other = PageTokens("k" * 40), PageTokens("j" * 40)
        t = tokens.make("tkt_one")
        self.assertTrue(tokens.ok("tkt_one", t))
        self.assertEqual(t, tokens.make("tkt_one"), "stateless: the same page gives the same token")
        self.assertFalse(tokens.ok("tkt_two", t), "another ticket")
        self.assertFalse(other.ok("tkt_one", t), "another server's key")
        self.assertFalse(tokens.ok("tkt_one", None))
        self.assertFalse(tokens.ok("tkt_one", ""))
        self.assertFalse(tokens.ok("tkt_one", t[:-1]))
        self.assertRegex(t, r"^[A-Za-z0-9_-]{43}$", "base64url of an HMAC-SHA256, no padding")


class KeptFromIssue149(unittest.TestCase):
    """#394's consumption tests are kept, adapted to the POST, in tests/test_deletion_link_consumption.py; this pins
    the one that can be stated in a line: two POSTs with one link get one redirect."""

    def test_two_posts_with_one_link_get_one_redirect(self):
        from inkcast.services.identity.login import OIDCIdentity
        provider = Provider(up=True)
        app, ctx = keycloak_app(provider)
        self.addCleanup(ctx.store.close)
        ticket = a_link(ctx)
        barrier = asyncio.Barrier(2)
        real_start = OIDCIdentity.start

        async def start_then_wait(self, request, pending_id):
            response = await real_start(self, request, pending_id)
            await asyncio.wait_for(barrier.wait(), timeout=5)
            return response

        async def two_tabs():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC_URL) as c:
                csrf = csrf_of((await c.get(f"/account/delete/{ticket}")).text)
                with mock.patch.object(OIDCIdentity, "start", start_then_wait):
                    return await asyncio.gather(c.post(f"/account/delete/{ticket}", data={"csrf": csrf}),
                                                c.post(f"/account/delete/{ticket}", data={"csrf": csrf}))

        first, second = asyncio.run(two_tabs())
        self.assertEqual(sorted((first.status_code, second.status_code)), [302, 400])
        self.assertEqual(len(pending_rows(ctx)), 1)
        self.assertIsNone(link_hash(ctx))


if __name__ == "__main__":
    unittest.main()
