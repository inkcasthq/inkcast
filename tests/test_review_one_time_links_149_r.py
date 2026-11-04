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


if __name__ == "__main__":
    unittest.main()
