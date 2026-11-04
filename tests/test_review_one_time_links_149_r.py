# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #394 (issue #149): one-time links in logs, and the deletion link used up only once the
sign-in redirect is ready. One class per finding."""

import asyncio
import contextlib
import io
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx2

from inkcast.libs.observability import setup_logging

COOKIE_VALUE = "consentCookieValue_0123456789abcdef"     # what auth_callback's Set-Cookie carries through the Gateway


class _Upstream(BaseHTTPRequestHandler):
    """An upstream service answering the way /auth/callback does: a 303 that sets the consent cookie."""

    def do_GET(self):
        self.send_response(303)
        self.send_header("Location", "/auth/consent?pending=pnd_abcdefgh")
        self.send_header("Set-Cookie", f"inkcast_consent_0123456789abcdef={COOKIE_VALUE}; HttpOnly; Path=/")
        self.end_headers()

    def log_message(self, *args):                       # http.server's own stderr line
        pass


class DebugLogsKeepNoCookies(unittest.TestCase):
    """At INKCAST_LOG_LEVEL=DEBUG, httpcore2 (under every httpx2 client: the Gateway's upstream calls, Keycloak's)
    writes each response's headers into a debug line, Set-Cookie values included; the patterns do not mask them. The
    consent cookie with the pending id beside it in Location is what /auth/consent asks of the browser that signed in."""

    def setUp(self):
        root = logging.getLogger()
        saved = (root.handlers[:], root.level,
                 {n: logging.getLogger(n).level for n in ("httpx2", "httpcore2", "uvicorn.access")})

        def restore():
            root.handlers[:], root.level = saved[0], saved[1]
            for name, level in saved[2].items():
                logging.getLogger(name).setLevel(level)
        self.addCleanup(restore)
        server = HTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}/auth/callback"

    def test_a_cookie_in_an_upstream_response_reaches_no_debug_line(self):
        for fmt in ("json", "text"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup_logging(fmt, "DEBUG", service="gateway")

            async def call():
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    return await client.get(self.url, headers={"cookie": f"inkcast_consent_x={COOKIE_VALUE}"})
            response = asyncio.run(call())
            self.assertEqual(response.status_code, 303)
            self.assertNotIn(COOKIE_VALUE, out.getvalue(), f"{fmt}: a cookie value in a debug line")


class TheRouteWalkSeesEveryParameter(unittest.TestCase):
    """test_credential_path_redaction fails on a path parameter it cannot classify, so the next one-time route cannot
    be missed. It finds the parameters with its own pattern; Starlette's grammar is wider (capitals and digits), and a
    parameter the walk does not see is neither classified nor checked for masking."""

    def test_the_walk_finds_the_parameters_starlette_finds(self):
        from starlette.routing import compile_path

        from test_credential_path_redaction import PARAM
        for template in ("/share/{shareTicket}", "/invite/{ticket2}/accept", "/connect/{ticket:str}", "/x/{post_id}"):
            self.assertEqual(PARAM.findall(template), list(compile_path(template)[2]), template)


class ProviderDownAfterARecentSignIn(unittest.TestCase):
    """OIDCIdentity.discovery keeps Keycloak's metadata an hour. After any sign-in in that hour, a visit while Keycloak
    is down raises no LoginError: the redirect is built from the cache, the link is used up, and the browser is sent to
    a provider that does not answer. Issue #149's test, "a deletion link opened while the sign-in provider is
    unreachable can be opened again", then fails on a server that has been in use."""

    def test_a_link_opened_while_the_provider_is_down_can_be_opened_again_after_a_recent_sign_in(self):
        from starlette.testclient import TestClient

        from test_deletion_link_consumption import AUTHORIZE, PUBLIC_URL, Provider, a_link, keycloak_app
        provider = Provider(up=True)
        app, ctx = keycloak_app(provider)
        with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
            self.assertEqual(c.get(f"/account/delete/{a_link(ctx)}").status_code, 302, "a sign-in this hour")
            provider.up = False
            ticket = a_link(ctx)
            down = c.get(f"/account/delete/{ticket}")
            self.assertEqual(down.status_code, 502, f"sent to {down.headers.get('location')}")
            self.assertIsNotNone(ctx.store.deletion_request_by_link(ticket), "the link is not used up")
            provider.up = True
            back = c.get(f"/account/delete/{ticket}")
            self.assertEqual(back.status_code, 302, back.text)
            self.assertTrue(back.headers["location"].startswith(AUTHORIZE + "?"))
            self.assertIsNone(ctx.store.deletion_request_by_link(ticket), "used up now, with the redirect ready")


if __name__ == "__main__":
    unittest.main()
