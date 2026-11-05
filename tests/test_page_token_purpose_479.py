# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A one-time page's token is bound to the page as well as the ticket; a route limited with HEAD alone counts HEAD
(issue #479).

468-1: ``PageTokens`` was HMAC-SHA256 over the ticket alone, under one key for every page, so the connect page and the
deletion page gave the same token for the same ticket string: a form served by one page answered the other. Harmless
while the two tables hold independent random tickets, inherited by any later page that reuses another's ticket.

469-2: ``_Guarded`` turned a HEAD into a GET before it looked at the route's set, so a set naming HEAD alone never
counted HEAD: at a limit of 3 a minute, ``{"HEAD"}`` x5 gave five 200s."""

import base64
import hashlib
import hmac
import re
import unittest
from unittest import mock

import httpx2
from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http.app import compose
from inkcast.libs.http.onetime import PageTokens
from inkcast.libs.http.ratelimit import RateLimiter, limited
from inkcast.libs.secrets import derive_key
from test_connect_tickets_399 import Dev
from test_deletion_link_consumption import AUTHORIZE, ISSUER, PUBLIC_URL, Provider
from test_ratelimit_head_400 import _call, _Ok

KEY = "k" * 40
SHARED = "tkt_the_same_string_in_both_tables"


def csrf_of(page: str) -> str:
    return re.search(r'name=csrf value="([^"]*)"', page).group(1)


class TheTokenIsBoundToThePage(unittest.TestCase):
    def test_one_pages_token_is_refused_by_the_other_for_the_same_ticket(self):
        connect, delete = PageTokens(KEY, "connect"), PageTokens(KEY, "delete-account")
        self.assertNotEqual(connect.make(SHARED), delete.make(SHARED))
        self.assertFalse(delete.ok(SHARED, connect.make(SHARED)))
        self.assertFalse(connect.ok(SHARED, delete.make(SHARED)))
        self.assertTrue(connect.ok(SHARED, connect.make(SHARED)))
        self.assertTrue(delete.ok(SHARED, PageTokens(KEY, "delete-account").make(SHARED)), "stateless per page")

    def test_a_purpose_and_a_ticket_cannot_trade_characters(self):
        """The purpose is in the key, not glued to the ticket: ("connect", "x") and ("connec", "tx") differ."""
        self.assertNotEqual(PageTokens(KEY, "connect").make("x"), PageTokens(KEY, "connec").make("tx"))
        self.assertNotEqual(PageTokens(KEY, "connect").make(":x"), PageTokens(KEY, "connect:").make("x"))


class ThroughBothPages(unittest.TestCase):
    """The connect page and the deletion page, one app, one ticket string in both tables."""

    def setUp(self):
        provider, dev = Provider(up=True), Dev()
        keycloak = provider.transport()

        def handle(request: httpx2.Request) -> httpx2.Response:
            return keycloak.handle_request(request) if request.url.host == "sso.example.com" else dev(request)

        cfg = Config(public_url=PUBLIC_URL, secret_key=KEY, database_path=":memory:", login="oidc", oidc_issuer=ISSUER,
                     oidc_client_id="inkcast", oidc_client_secret="kc-secret", keycloak_sync_client_id="inkcast-sync",
                     keycloak_sync_secret="sync-secret")
        specs = resolve("IdentityService,ConnectorService")
        self.ctx = ctx = build(cfg, specs, transport=httpx2.MockTransport(handle))
        self.addCleanup(ctx.store.close)
        self.app, self.dev = compose(specs, ctx), dev
        user = ctx.store.upsert_user(f"oidc:{ISSUER}#owner", "owner", None, None)["id"]
        with mock.patch("secrets.token_urlsafe", return_value=SHARED):  # the same ticket string in both tables
            self.assertEqual(ctx.store.create_ticket(user, "devto", ttl=900), SHARED)
            self.assertEqual(ctx.store.create_deletion_request(user, ttl=900), SHARED)

    def test_each_page_refuses_the_others_token_and_accepts_its_own_shown_again(self):
        with TestClient(self.app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            connects = csrf_of(c.get(f"/connect/{SHARED}").text)
            deletes = csrf_of(c.get(f"/account/delete/{SHARED}").text)
            r = c.post(f"/account/delete/{SHARED}", data={"csrf": connects})
            self.assertEqual((r.status_code, "Request rejected" in r.text), (400, True), "the connect page's token")
            r = c.post(f"/connect/{SHARED}", data={"api_key": "wrong", "csrf": deletes})
            self.assertEqual((r.status_code, "Request rejected" in r.text), (400, True), "the deletion page's token")
            self.assertEqual(self.dev.verified, 0, "DEV was not asked")
            self.assertIsNotNone(self.ctx.store.deletion_request_by_link(SHARED), "both links are still good")
            self.assertIsNotNone(self.ctx.store.get_ticket(SHARED))

            self.assertEqual(csrf_of(c.get(f"/connect/{SHARED}").text), connects, "the page shown again, same token")
            r = c.post(f"/connect/{SHARED}", data={"api_key": "wrong", "csrf": connects})
            self.assertEqual(r.status_code, 200, "its own token passes: DEV is asked and refuses the key")
            self.assertEqual(self.dev.verified, 1)
            self.assertEqual(csrf_of(c.get(f"/account/delete/{SHARED}").text), deletes)
            r = c.post(f"/account/delete/{SHARED}", data={"csrf": deletes})
            self.assertEqual(r.status_code, 302, r.text[:300])
            self.assertTrue(r.headers["location"].startswith(AUTHORIZE + "?"))

    def test_a_form_served_before_the_change_is_refused_and_the_link_reopens(self):
        """A page open across the deploy carries the old token (one key for every page): the POST is refused with the
        page's own "open the link again", nothing is used up, and the link opened again works within its lifetime."""
        old = base64.urlsafe_b64encode(hmac.new(derive_key(KEY, "one-time-page"), SHARED.encode(), hashlib.sha256)
                                       .digest()).decode().rstrip("=")
        with TestClient(self.app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            r = c.post(f"/account/delete/{SHARED}", data={"csrf": old})
            self.assertEqual(r.status_code, 400, r.text[:300])
            self.assertIn("The form was invalid. Open the link again.", r.text)
            self.assertIsNotNone(self.ctx.store.deletion_request_by_link(SHARED), "the link is still good")
            r = c.post(f"/account/delete/{SHARED}", data={"csrf": csrf_of(c.get(f"/account/delete/{SHARED}").text)})
            self.assertEqual(r.status_code, 302, "opened again, it works")


class AHeadOnlySet(unittest.TestCase):
    def test_a_route_limited_with_head_alone_counts_head(self):
        app = limited(_Ok(), RateLimiter(per_minute=3), "x", {"HEAD"})
        self.assertEqual([_call(app, "HEAD") for _ in range(5)], [200, 200, 200, 429, 429])

    def test_and_only_head(self):
        """The set names HEAD, not GET: a GET is not counted, as a POST is not."""
        app = limited(_Ok(), RateLimiter(per_minute=1), "x", {"HEAD"})
        self.assertEqual([_call(app, m) for m in ("GET", "POST", "GET", "HEAD", "HEAD")], [200, 200, 200, 200, 429])

    def test_head_and_get_named_together_draw_on_one_bucket(self):
        app = limited(_Ok(), RateLimiter(per_minute=3), "x", {"HEAD", "GET"})
        self.assertEqual([_call(app, m) for m in ("GET", "HEAD", "GET", "HEAD")], [200, 200, 200, 429])


if __name__ == "__main__":
    unittest.main()
