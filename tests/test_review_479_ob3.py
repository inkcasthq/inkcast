# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of issue #479: the page tokens bound to their page, and the limiter's method sets."""

import asyncio
import base64
import hashlib
import hmac
import itertools
import logging
import pathlib
import re
import unittest
from unittest import mock

import httpx2
from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http import onetime
from inkcast.libs.http.app import compose
from inkcast.libs.http.onetime import PageTokens
from inkcast.libs.http.ratelimit import WRITE_METHODS, RateLimiter, _Guarded, limited
from inkcast.libs.secrets import derive_key
from test_connect_tickets_399 import GOOD_KEY, Dev
from test_deletion_link_consumption import AUTHORIZE, ISSUER, PUBLIC_URL, Provider
from test_ratelimit_head_400 import _call, _Ok

KEY = "k" * 40
SHARED = "tkt_ob3_the_same_string_in_both_tables"
SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "inkcast"


def csrf_of(page: str) -> str:
    return re.search(r'name=csrf value="([^"]*)"', page).group(1)


def old_token(ticket: str) -> str:
    """The token a form served before #479 carries: one key, "one-time-page", for every page."""
    digest = hmac.new(derive_key(KEY, "one-time-page"), ticket.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class BothPages(unittest.TestCase):
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
        self.user = ctx.store.upsert_user(f"oidc:{ISSUER}#owner", "owner", None, None)["id"]
        with mock.patch("secrets.token_urlsafe", return_value=SHARED):
            self.assertEqual(ctx.store.create_ticket(self.user, "devto", ttl=900), SHARED)
            self.assertEqual(ctx.store.create_deletion_request(self.user, ttl=900), SHARED)

    def pending_rows(self) -> int:
        return self.ctx.store._one("SELECT COUNT(*) FROM pending_auth")[0]


class C1_EveryPathBindsThePage(BothPages):
    def test_every_constructor_names_its_own_page(self):
        """Every ``PageTokens(`` in src names a purpose, and no two name the same one."""
        calls = [m for p in SRC.rglob("*.py") for m in re.findall(r"PageTokens\(([^)]*)\)", p.read_text())]
        purposes = [re.search(r',\s*"([^"]+)"\s*$', c).group(1) for c in calls]
        self.assertEqual(sorted(purposes), ["connect", "delete-account"])

    def test_head_serves_the_pages_own_token_and_changes_nothing(self):
        with TestClient(self.app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            self.assertEqual(c.head(f"/connect/{SHARED}").status_code, 200)
            self.assertEqual(c.head(f"/account/delete/{SHARED}").status_code, 200)
            connects, deletes = csrf_of(c.get(f"/connect/{SHARED}").text), csrf_of(c.get(f"/account/delete/{SHARED}").text)
            self.assertEqual(connects, PageTokens(KEY, "connect").make(SHARED))
            self.assertEqual(deletes, PageTokens(KEY, "delete-account").make(SHARED))
            r = c.post(f"/connect/{SHARED}", data={"api_key": GOOD_KEY, "csrf": deletes})
            self.assertEqual(r.status_code, 400)
            self.assertIsNone(self.ctx.store.get_credentials(self.user, "devto"), "a good key, the other page's token")
            r = c.post(f"/connect/{SHARED}", data={"api_key": GOOD_KEY, "csrf": connects})
            self.assertEqual(r.status_code, 200, r.text[:300])
            self.assertIsNotNone(self.ctx.store.get_credentials(self.user, "devto"))


class C3_AFormOpenedBeforeTheDeploy(BothPages):
    def test_connect_refuses_it_and_nothing_is_verified_used_up_or_saved(self):
        with TestClient(self.app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            r = c.post(f"/connect/{SHARED}", data={"api_key": GOOD_KEY, "csrf": old_token(SHARED)})
            self.assertEqual(r.status_code, 400)
            self.assertIn("The form was invalid. Open the link again.", r.text)
            self.assertEqual(self.dev.verified, 0)
            self.assertIsNotNone(self.ctx.store.get_ticket(SHARED), "the link is still good")
            self.assertIsNone(self.ctx.store.get_credentials(self.user, "devto"), "no credentials saved")
            fresh = csrf_of(c.get(f"/connect/{SHARED}").text)
            r = c.post(f"/connect/{SHARED}", data={"api_key": GOOD_KEY, "csrf": fresh})
            self.assertEqual(r.status_code, 200, "opened again, it works")
            self.assertIsNone(self.ctx.store.get_ticket(SHARED), "and only then is it used up")

    def test_deletion_refuses_it_and_no_pending_is_created(self):
        before = self.pending_rows()
        with TestClient(self.app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            r = c.post(f"/account/delete/{SHARED}", data={"csrf": old_token(SHARED)})
            self.assertEqual(r.status_code, 400)
            self.assertIn("The form was invalid. Open the link again.", r.text)
            self.assertEqual(self.pending_rows(), before, "no pending sign-in created")
            self.assertIsNotNone(self.ctx.store.deletion_request_by_link(SHARED))
            r = c.post(f"/account/delete/{SHARED}", data={"csrf": csrf_of(c.get(f"/account/delete/{SHARED}").text)})
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["location"].startswith(AUTHORIZE + "?"))
            self.assertEqual(self.pending_rows(), before + 1)


class C2_TheDerivedKeysCannotCollide(unittest.TestCase):
    def labels(self) -> list[str]:
        """Every derive_key label in src, the f-string one expanded with each page's purpose."""
        found = [m for p in SRC.rglob("*.py") for m in re.findall(r'derive_key\([^,]+,\s*f?"([^"]+)"\)', p.read_text())]
        self.assertIn("one-time-page:{purpose}", found)
        return [lab for f in found for lab in
                ([f.format(purpose=p) for p in ("connect", "delete-account")] if "{" in f else [f])]

    def test_no_prefix_is_a_prefix_of_another_so_no_two_keys_agree_even_across_secrets(self):
        labels = self.labels()
        self.assertEqual(sorted(labels), ["credentials", "erasure", "one-time-page:connect", "one-time-page:delete-account"])
        prefixes = [f"inkcast-{lab}:" for lab in labels]
        for a, b in itertools.permutations(prefixes, 2):
            self.assertFalse(b.startswith(a), (a, b))
        self.assertEqual(len({derive_key(KEY, lab) for lab in labels}), len(labels))
        self.assertNotIn(derive_key(KEY, "one-time-page"), {derive_key(KEY, lab) for lab in labels})


class C4_TheLimitersSets(unittest.TestCase):
    METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "get", "head", None)
    SETS = (WRITE_METHODS, {"GET"}, {"HEAD"}, {"GET", "HEAD"}, {"GET", "POST"}, {"POST"}, set())

    @staticmethod
    def base(method, methods) -> bool:
        """ca058dbe's rule: a HEAD became a GET before the set was asked."""
        return ("GET" if method == "HEAD" else method) in methods

    def counted(self, method, methods) -> bool:
        limiter = mock.Mock()
        limiter.check = mock.AsyncMock(return_value=0)
        scope = {"type": "http", "client": ("10.0.0.1", 1)}
        if method is not None:
            scope["method"] = method
        asyncio.run(_Guarded(_Ok(), limiter, "x", methods)(scope, mock.AsyncMock(), mock.AsyncMock()))
        return limiter.check.await_count == 1

    def test_only_a_head_only_set_changed(self):
        for method, methods in itertools.product(self.METHODS, self.SETS):
            want = self.base(method, methods) or (method == "HEAD" and "HEAD" in methods)
            self.assertEqual(self.counted(method, methods), want, (method, sorted(methods)))

    def test_websocket_and_lifespan_never_reach_the_limiter(self):
        for scope in ({"type": "websocket", "path": "/x"}, {"type": "lifespan"}, {"type": "websocket", "method": "GET"}):
            limiter = mock.Mock()
            limiter.check = mock.AsyncMock(side_effect=AssertionError("asked"))
            app = mock.AsyncMock()
            asyncio.run(_Guarded(app, limiter, "x", {"GET", "HEAD", "POST"})(scope, mock.AsyncMock(), mock.AsyncMock()))
            app.assert_awaited_once()

    def test_get_still_counts_head(self):
        app = limited(_Ok(), RateLimiter(per_minute=2), "x", {"GET"})
        self.assertEqual([_call(app, m) for m in ("HEAD", "GET", "HEAD")], [200, 200, 429])


class C5_NoSecretLoggedAndConstantTime(BothPages):
    def test_ok_compares_in_constant_time(self):
        tokens = PageTokens(KEY, "connect")
        with mock.patch.object(onetime.hmac, "compare_digest", wraps=hmac.compare_digest) as cd:
            self.assertTrue(tokens.ok("t", tokens.make("t")))
            self.assertFalse(tokens.ok("t", None))
            self.assertFalse(tokens.ok("t", "x" * 43))
        self.assertEqual(cd.call_count, 3)

    def test_no_key_or_token_in_any_log_record(self):
        secrets = [KEY, PageTokens(KEY, "connect").make(SHARED), PageTokens(KEY, "delete-account").make(SHARED),
                   old_token(SHARED), derive_key(KEY, "one-time-page:connect").hex()]
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("ob3").debug("start")
            with TestClient(self.app, base_url=PUBLIC_URL, follow_redirects=False) as c:
                for path in (f"/connect/{SHARED}", f"/account/delete/{SHARED}"):
                    token = csrf_of(c.get(path).text)
                    c.post(path, data={"api_key": "wrong", "csrf": token})
                    c.post(path, data={"api_key": "wrong", "csrf": old_token(SHARED)})
        text = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in logs.records)
        for s in secrets:
            self.assertNotIn(s, text)


if __name__ == "__main__":
    unittest.main()
