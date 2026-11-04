# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The per-client rate limit counts a HEAD as the GET it is (issue #400).

Starlette answers HEAD on every GET route, and a HEAD runs the same handler as the GET (RFC 9110 §9.3.2: "identical to
GET except that the server MUST NOT send content"), so a HEAD on a limited GET route costs what the GET costs: a
pending row on /tokens/new, a discovery request and a pending row on the deletion link's Continue. ``_Guarded`` checked
the request method against the route's configured set, which names GET, never HEAD, so HEADs were never counted: at
a limit of 3 a minute, GET x10 gave three 200s and a 429, HEAD x10 gave ten 200s and ten pending rows."""

import asyncio
import unittest

from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http.app import compose
from inkcast.libs.http.ratelimit import RateLimiter, limited

PUBLIC_URL = "http://localhost:8787"


class _Ok:
    """An ASGI app (an instance, so ``limited`` wraps it as one) that answers 200 to everything."""

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _call(app, method: str) -> int:
    """The status an ASGI app answers to one request with ``method`` from one client."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": "/x", "headers": [], "client": ("10.0.0.1", 1234),
             "query_string": b"", "scheme": "http", "server": ("localhost", 8787)}
    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


class TheGuard(unittest.TestCase):
    def test_a_head_is_counted_as_a_get_on_a_limited_get_route(self):
        app = limited(_Ok(), RateLimiter(per_minute=3), "x", {"GET"})
        self.assertEqual([_call(app, "HEAD") for _ in range(4)], [200, 200, 200, 429])

    def test_get_and_head_draw_on_one_bucket(self):
        app = limited(_Ok(), RateLimiter(per_minute=3), "x", {"GET"})
        self.assertEqual([_call(app, m) for m in ("GET", "HEAD", "GET", "HEAD", "GET")], [200, 200, 200, 429, 429])

    def test_a_head_is_not_counted_where_the_get_is_not(self):
        """The default set is the writes: a HEAD on a route that limits only its POST costs nothing, as its GET costs
        nothing."""
        app = limited(_Ok(), RateLimiter(per_minute=1), "x")
        self.assertEqual([_call(app, m) for m in ("HEAD", "GET", "HEAD", "POST", "POST")], [200, 200, 200, 200, 429])


class ThroughTheStack(unittest.TestCase):
    """/tokens/new is a GET that writes (a pending sign-in), limited per client with ``{"GET"}``."""

    def client(self, per_minute: int) -> TestClient:
        cfg = Config(public_url=PUBLIC_URL, secret_key="k" * 40, database_path=":memory:", login="password",
                     password="p" * 12, rate_limit_per_minute=per_minute)
        specs = resolve("IdentityService,ConnectorService")
        ctx = build(cfg, specs)
        self.addCleanup(ctx.store.close)
        self.ctx = ctx
        return TestClient(compose(specs, ctx), base_url=PUBLIC_URL)

    def pending(self) -> int:
        return self.ctx.store._one("SELECT count(*) AS n FROM pending_auth")["n"]

    def test_head_x_n_on_a_limited_get_route_gets_a_429_at_the_limit_like_get(self):
        params = {"name": "ci", "scopes": "stories:sync"}
        with self.client(3) as c:
            heads = [c.head("/tokens/new", params=params) for _ in range(5)]
            self.assertEqual([r.status_code for r in heads], [200, 200, 200, 429, 429])
            self.assertIn("Retry-After", heads[-1].headers)
            self.assertEqual(self.pending(), 3, "three HEADs wrote their row; the limited ones wrote nothing")
        with self.client(3) as c:
            gets = [c.get("/tokens/new", params=params) for _ in range(5)]
            self.assertEqual([r.status_code for r in gets], [200, 200, 200, 429, 429], "the same as GET")

    def test_a_head_on_a_post_limited_route_is_free_like_its_get(self):
        with self.client(2) as c:
            self.assertEqual([c.head("/connect/nope").status_code for _ in range(4)], [400] * 4)
            self.assertEqual([c.get("/connect/nope").status_code for _ in range(4)], [400] * 4)
            self.assertEqual([c.post("/connect/nope", data={"api_key": "x"}).status_code for _ in range(3)], [400, 400, 429])


if __name__ == "__main__":
    unittest.main()
