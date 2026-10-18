"""End to end over real HTTP: an MCP client signs in exactly like Claude Code (discovery,
dynamic registration, PKCE) through a Keycloak-style IdP and the consent page, connects
platforms through the browser connect page, then cross-posts, schedules and cancels."""

import asyncio
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import httpx2
import uvicorn

from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

from fakes import Fakes
from inkcast.config import Config
from inkcast.server import asgi_app, build

STORY = """---
title: Why we built an agent
subtitle: Lessons from year one
tags: [AI, agents, developer-tools, python, extra]
canonical_url: https://blog.example.com/why-we-built-an-agent
platforms: [medium, devto]
---

# Why we built an agent

The story body.
"""


_HANDED_OUT: set[int] = set()


def free_port() -> int:
    """A port nothing else in this test run was given. The socket is closed before the server binds it, so the OS
    may offer the same number again to the next caller (split mode asks for several in a row): never repeat one."""
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in _HANDED_OUT:
            _HANDED_OUT.add(port)
            return port


def serve(app, port, **options):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", **options))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        # uvicorn's thread ends at once when the port can't be bound: say so instead of waiting out the deadline.
        assert thread.is_alive(), f"server on port {port} exited before starting (port already in use?)"
        assert time.time() < deadline, f"server on port {port} did not start in 10 s"
        time.sleep(0.05)
    return server


class Memory:
    def __init__(self):
        self.tokens = self.client = None

    async def get_tokens(self): return self.tokens
    async def set_tokens(self, t): self.tokens = t
    async def get_client_info(self): return self.client
    async def set_client_info(self, c): self.client = c


class Browser:
    """Plays the person's browser through sign-in and consent."""

    def __init__(self, approve: bool = True):
        self.approve = approve
        self.result: dict = {}

    async def __call__(self, url: str) -> None:
        async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
            r = await c.get(url)
            while r.status_code in (302, 303) and "/auth/consent" not in r.headers["location"]:
                r = await c.get(urljoin(str(r.url), r.headers["location"]))
            consent_url = urljoin(str(r.url), r.headers["location"])
            page = await c.get(consent_url)
            self.result["consent_html"] = page.text
            fields = dict(re.findall(r'name=(\w+) value="([^"]*)"', page.text))
            r = await c.post(urljoin(consent_url, "/auth/consent"), data={
                "pending": fields["pending"], "csrf": fields["csrf"], "decision": "approve" if self.approve else "deny"})
            q = parse_qs(urlparse(r.headers["location"]).query)
            self.result.update(code=q.get("code", [None])[0], state=q.get("state", [None])[0], error=q.get("error", [None])[0])

    async def callback(self):
        return AuthorizationCodeResult(code=self.result["code"], state=self.result["state"])


class Harness(unittest.TestCase):
    """The running stack (fake IdP + platforms, Inkcast at public_url) and client helpers; no tests."""

    @classmethod
    def setUpClass(cls):
        fport, iport = free_port(), free_port()
        cls.tmp = tempfile.mkdtemp()
        cls.fakes = Fakes(f"http://127.0.0.1:{fport}")
        cls.config = Config(
            public_url=f"http://127.0.0.1:{iport}", port=iport, secret_key="k" * 48,
            database_path=os.path.join(cls.tmp, "inkcast.db"),
            login="oidc", oidc_issuer=cls.fakes.issuer, oidc_client_id="inkcast", oidc_client_secret="kc-secret",
            oidc_required_role="inkcast-user",
            platform_urls={"medium": f"{cls.fakes.base}/medium/v1", "devto": f"{cls.fakes.base}/devto/api"},
        )
        cls.config.validate()
        cls.app = build(cls.config)
        cls.servers = [serve(cls.fakes.app(), fport), serve(asgi_app(cls.app), iport)]

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.should_exit = True

    def oauth_client(self, browser: Browser, storage: "Memory | None" = None) -> OAuthClientProvider:
        return OAuthClientProvider(
            server_url=self.config.mcp_url,
            client_metadata=OAuthClientMetadata(client_name="Claude Code", redirect_uris=["http://localhost:53682/callback"],
                                                grant_types=["authorization_code", "refresh_token"],
                                                response_types=["code"], token_endpoint_auth_method="none"),
            storage=storage or Memory(), redirect_handler=browser, callback_handler=browser.callback,
        )

    async def run_tools(self, auth, steps):
        """steps: list of (tool, args) or callables(results) run between calls (e.g. a browser action)."""
        out = []
        async with httpx2.AsyncClient(auth=auth, timeout=30) as http:
            async with streamable_http_client(self.config.mcp_url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for step in steps:
                        if callable(step):
                            await step(out)
                            continue
                        name, args = step
                        r = await session.call_tool(name, args)
                        text = "\n".join(c.text for c in r.content if hasattr(c, "text"))
                        out.append(("ERROR: " if r.is_error else "") + text)
        return out

    async def fill_connect_page(self, link_text: str, fields: dict):
        link = re.search(r"(http://\S+/connect/\S+)", link_text).group(1)
        async with httpx2.AsyncClient() as c:
            page = await c.get(link)
            assert "never shown to the AI" in page.text
            done = await c.post(link, data=fields)
            again = await c.get(link)
        return done.text, again.status_code

    def access_token(self) -> str:
        storage = Memory()
        asyncio.run(self.run_tools(self.oauth_client(Browser(), storage), [("inkcast_platforms", {})]))
        return storage.tokens.access_token

    def insights_context(self):
        """The process that runs InsightsService (its outbox consumer is driven by hand in tests)."""
        return self.app


class EndToEnd(Harness):
    def test_discovery_points_clients_at_inkcast(self):
        async def run():
            async with httpx2.AsyncClient() as c:
                r = await c.post(self.config.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                 headers={"Accept": "application/json, text/event-stream"})
                prm = (await c.get(f"{self.config.public_url}/.well-known/oauth-protected-resource/mcp")).json()
                asm = (await c.get(f"{self.config.public_url}/.well-known/oauth-authorization-server")).json()
            return r.status_code, prm, asm
        status, prm, asm = asyncio.run(run())
        self.assertEqual(status, 401)
        self.assertEqual(prm["authorization_servers"], [self.config.public_url])
        self.assertIn("registration_endpoint", asm)
        self.assertEqual(asm["code_challenge_methods_supported"], ["S256"])

    def test_full_flow(self):
        browser = Browser()
        connect_results = {}

        def connect(platform, fields):
            async def step(out):
                connect_results[platform] = await self.fill_connect_page(out[-1], fields)
            return step

        future = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
        steps = [
            ("inkcast_platforms", {}),                                             # 0
            ("inkcast_publish", {"markdown": STORY, "dry_run": True}),             # 1
            ("inkcast_connect", {"platform_id": "devto"}),                         # 2
            connect("devto", {"api_key": "wrong"}),
            connect("devto", {"api_key": "devto-key"}),
            ("inkcast_connect", {"platform_id": "medium"}),                        # 3
            connect("medium", {"token": "medium-token"}),
            ("inkcast_platforms", {}),                                             # 4
            ("inkcast_destinations", {"platform_id": "medium"}),                   # 5
            ("inkcast_publish", {"markdown": STORY}),                              # 6 now, both
            ("inkcast_publish", {"markdown": STORY, "publish_at": future.isoformat(),   # 7 scheduled
                                 "destinations": {"medium": "better-programming", "devto": "analytical"}}),
            ("inkcast_publish", {"markdown": STORY, "destinations": {"medium": "Nope Weekly"}}),  # 8
            ("inkcast_publish", {"markdown": STORY, "platforms": ["myspace"]}),    # 9
        ]
        out = asyncio.run(self.run_tools(self.oauth_client(browser), steps))

        # Sign-in went through the IdP and our consent page, as the Keycloak user.
        self.assertIn("Allow Claude Code to use Inkcast?", browser.result["consent_html"])
        self.assertIn("ada", browser.result["consent_html"])
        self.assertIn("not connected", out[0])
        self.assertIn("NOT CONNECTED", out[1])
        self.assertIn("Import a story", out[1])  # Medium manual fallback
        self.assertIn("Medium uses only the first 3 tags", out[1])

        # Connect page: wrong key shows the error and keeps the link; right key consumes it.
        self.assertIn("connected as", out[4])
        self.assertIn("Better Programming", out[5])
        self.assertEqual(connect_results["devto"][1], 400)  # link used up after success

        now_out = out[6]
        self.assertIn("medium: published → https://medium.com/@ada/post-1", now_out)
        self.assertIn("devto: published → https://dev.to/ada/article-1", now_out)
        path, medium_body = self.fakes.medium_posts[0]
        self.assertEqual(path, "/medium/v1/users/m-ada/posts")
        self.assertTrue(medium_body["content"].startswith("# Why we built an agent\n\n## Lessons from year one\n\nThe story body."))
        self.assertEqual(medium_body["content"].count("# Why we built an agent"), 1)
        self.assertEqual(medium_body["tags"], ["AI", "agents", "developer-tools"])
        self.assertEqual(medium_body["canonicalUrl"], "https://blog.example.com/why-we-built-an-agent")
        article = self.fakes.devto_articles[0]
        self.assertEqual(article["description"], "Lessons from year one")
        self.assertEqual(article["tags"], ["ai", "agents", "developertools", "python"])
        self.assertNotIn("# Why we built an agent", article["body_markdown"])
        self.assertTrue(article["published"])

        self.assertIn("medium: scheduled for", out[7])
        self.assertIn("devto: scheduled for", out[7])
        self.assertIn("FAILED", out[8])
        self.assertIn("Better Programming", out[8])
        self.assertTrue(out[9].startswith("ERROR") and "myspace" in out[9])

        # A separate scheduler process (own store + HTTP client) sends the due posts.
        worker = build(self.config)
        self.fakes.devto_fail_next = 429
        async def two_ticks():  # one event loop, like the real scheduler
            first = await worker.publisher.run_due(now=future.timestamp() + 5)
            second = await worker.publisher.run_due(now=time.time() + 120)
            await worker.http.aclose()
            return first, second
        due, retry = asyncio.run(two_ticks())
        by_platform = {o.platform: o for o in due}
        self.assertEqual(by_platform["medium"].status, "draft")  # writer in the publication → draft
        self.assertIn("writer", by_platform["medium"].notes[0])
        self.assertEqual(self.fakes.medium_posts[-1][0], "/medium/v1/publications/pub-bp/posts")
        self.assertEqual(self.fakes.medium_posts[-1][1]["publishStatus"], "draft")
        self.assertEqual(by_platform["devto"].status, "scheduled")  # 429 → retry later
        self.assertEqual([o.status for o in retry], ["published"])
        self.assertEqual(len(self.fakes.devto_articles), 3)  # now, the partial-failure run, the retry
        self.assertEqual(self.fakes.devto_articles[-1]["organization_id"], 7)  # "analytical" resolved to its id

        # List and cancel.
        future2 = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        out2 = asyncio.run(self.run_tools(self.oauth_client(Browser()), [
            ("inkcast_publish", {"markdown": STORY, "platforms": ["devto"], "publish_at": future2}),
            ("inkcast_posts", {}),
        ]))
        post_id = re.search(r"post `(pst_\w+)`", out2[0]).group(1)
        out3 = asyncio.run(self.run_tools(self.oauth_client(Browser()), [
            ("inkcast_cancel", {"post_id": post_id}),
            ("inkcast_cancel", {"post_id": post_id}),
            ("inkcast_posts", {}),
        ]))
        self.assertIn("[published]", out2[1])
        self.assertIn("[draft]", out2[1])
        self.assertIn("Cancelled", out3[0])
        self.assertTrue(out3[1].startswith("ERROR"))
        self.assertIn("[cancelled]", out3[2])

    def test_rest_api(self):
        token = self.access_token()
        base = self.config.public_url

        async def run():
            h = {"Authorization": f"Bearer {token}"}
            async with httpx2.AsyncClient(timeout=30) as c:
                anon = await c.get(f"{base}/api/v1/platforms")
                bad = await c.get(f"{base}/api/v1/platforms", headers={"Authorization": "Bearer nope"})
                me = (await c.get(f"{base}/api/v1/me", headers=h)).json()
                plats = (await c.get(f"{base}/api/v1/platforms", headers=h)).json()
                link = (await c.post(f"{base}/api/v1/platforms/devto/connect-link", headers=h)).json()
                await c.post(link["url"], data={"api_key": "devto-key"})
                preview = await c.post(f"{base}/api/v1/posts", headers=h, json={"markdown": STORY, "dry_run": True})
                invalid = await c.post(f"{base}/api/v1/posts", headers=h, json={"markdown": "x", "platforms": ["devto"]})
                notjson = await c.post(f"{base}/api/v1/posts", headers=h, content=b"{")
                sched = await c.post(f"{base}/api/v1/posts", headers=h, json={
                    "markdown": STORY, "platforms": ["devto"],
                    "publish_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()})
                pid = sched.json()["results"][0]["post_id"]
                listed = (await c.get(f"{base}/api/v1/posts?days_ahead=30", headers=h)).json()
                bad_q = await c.get(f"{base}/api/v1/posts?days_ahead=9999", headers=h)
                cancelled = await c.post(f"{base}/api/v1/posts/{pid}/cancel", headers=h)
                again = await c.post(f"{base}/api/v1/posts/{pid}/cancel", headers=h)
                missing = await c.post(f"{base}/api/v1/posts/pst_nope/cancel", headers=h)
                unknown_platform = await c.post(f"{base}/api/v1/platforms/myspace/connect-link", headers=h)
            return locals()
        r = asyncio.run(run())
        self.assertEqual(r["anon"].status_code, 401)
        self.assertIn("resource_metadata=", r["anon"].headers["www-authenticate"])
        self.assertEqual(r["bad"].status_code, 401)
        self.assertEqual(r["me"]["display"], "ada")
        self.assertEqual({p["id"] for p in r["plats"]}, {"medium", "devto"})
        self.assertTrue(r["link"]["url"].startswith(f"{base}/connect/"))
        prev = r["preview"].json()
        self.assertTrue(prev["dry_run"])
        self.assertEqual([x["status"] for x in prev["results"]], ["preview", "preview"])
        self.assertEqual(r["invalid"].status_code, 422)
        self.assertEqual(r["invalid"].json()["error"], "invalid_story")
        self.assertEqual(r["notjson"].status_code, 422)
        self.assertEqual(r["sched"].json()["results"][0]["status"], "scheduled")
        self.assertIn(r["pid"], {p["id"] for p in r["listed"]})
        self.assertEqual(r["bad_q"].status_code, 422)
        self.assertEqual(r["cancelled"].json()["status"], "cancelled")
        self.assertEqual(r["again"].status_code, 409)
        self.assertEqual(r["missing"].status_code, 404)
        self.assertEqual(r["unknown_platform"].status_code, 404)
        self.assertEqual(r["sched"].headers["cache-control"], "no-store")

    def test_insights_are_scoped_to_the_caller_and_come_from_rollups(self):
        token = self.access_token()
        base = self.config.public_url

        async def run():
            h = {"Authorization": f"Bearer {token}"}
            from inkcast.services.insights import poller
            async with httpx2.AsyncClient(timeout=30) as c:
                link = (await c.post(f"{base}/api/v1/platforms/devto/connect-link", headers=h)).json()
                await c.post(link["url"], data={"api_key": "devto-key"})
                poller(self.insights_context()).drain()                  # other tests of this class emitted events too
                start = await c.get(f"{base}/api/v1/insights/summary?days=7", headers=h)
                start_series = await c.get(f"{base}/api/v1/insights/kpis?names=posts_published,posts_cancelled,sync_results"
                                           "&grain=hour", headers=h)
                now = await c.post(f"{base}/api/v1/posts", headers=h, json={"markdown": STORY, "platforms": ["devto"]})
                sched = await c.post(f"{base}/api/v1/posts", headers=h, json={
                    "markdown": STORY, "platforms": ["devto"],
                    "publish_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()})
                await c.post(f"{base}/api/v1/posts/{sched.json()['results'][0]['post_id']}/cancel", headers=h)
                synced = await c.post(f"{base}/api/v1/stories:sync", headers=h, json={
                    "source": "github.com/acme/blog", "files": [{"path": "a.md", "markdown": STORY.replace(
                        "platforms: [medium, devto]", "platforms: [devto]")}]})
                pat = (await c.post(f"{base}/api/v1/tokens", headers=h, json={"name": "ci", "scopes": ["posts:read"]})).json()
                before = await c.get(f"{base}/api/v1/insights/summary?days=7", headers=h)
                poller(self.insights_context()).drain()                  # what the worker does every few seconds
                summary = await c.get(f"{base}/api/v1/insights/summary?days=7", headers=h)
                series = await c.get(f"{base}/api/v1/insights/kpis?names=posts_published,posts_cancelled,sync_results&grain=hour",
                                     headers=h)
                unknown = await c.get(f"{base}/api/v1/insights/kpis?names=mrr", headers=h)
                bad_grain = await c.get(f"{base}/api/v1/insights/kpis?names=posts_published&grain=week", headers=h)
                no_scope = await c.get(f"{base}/api/v1/insights/summary", headers={"Authorization": f"Bearer {pat['token']}"})
                anon = await c.get(f"{base}/api/v1/insights/summary")
            return locals()
        r = asyncio.run(run())
        self.assertEqual(r["now"].json()["results"][0]["status"], "published")
        self.assertEqual(r["synced"].json()["results"][0]["action"], "create")

        def count(summary, status):
            return summary["posts_by_status"].get(status, {}).get("devto", 0)

        def total(series_response, name, **dims):
            [series] = [x for x in series_response.json() if x["name"] == name]
            return sum(p["value"] for p in series["points"] if all(p["dims"].get(k) == v for k, v in dims.items()))

        s0, before, s = r["start"].json(), r["before"].json(), r["summary"].json()
        self.assertEqual(count(before, "published") - count(s0, "published"), 2)   # live counts: no rollup needed
        self.assertEqual((s["scope"], s["days"]), ("user", 7))
        self.assertEqual(count(s, "cancelled") - count(s0, "cancelled"), 1)
        self.assertEqual(s["ledger_by_status"].get("published", 0) - s0["ledger_by_status"].get("published", 0), 1)
        devto = [x for x in s["publish_success"] if x["platform"] == "devto"][0]
        devto0 = ([x for x in s0["publish_success"] if x["platform"] == "devto"] or [{"numerator": 0, "denominator": 0}])[0]
        self.assertEqual((devto["numerator"] - devto0["numerator"], devto["denominator"] - devto0["denominator"]), (2, 2))
        self.assertEqual(devto["rate"], devto["numerator"] / devto["denominator"])
        self.assertEqual(s["sync_by_action"].get("create", 0) - s0["sync_by_action"].get("create", 0), 1)
        self.assertEqual(s["sync_by_platform"].get("devto", 0) - s0["sync_by_platform"].get("devto", 0), 1)
        self.assertEqual(total(r["series"], "posts_published", platform="devto", status="published")
                         - total(r["start_series"], "posts_published", platform="devto", status="published"), 2)
        self.assertEqual(total(r["series"], "posts_cancelled") - total(r["start_series"], "posts_cancelled"), 1)
        self.assertEqual(total(r["series"], "sync_results", action="create") - total(r["start_series"], "sync_results", action="create"), 1)
        self.assertEqual(r["unknown"].status_code, 422)                    # owner-only KPIs are not served here
        self.assertEqual(r["bad_grain"].status_code, 422)
        self.assertEqual((r["no_scope"].status_code, r["no_scope"].json()["error"]), (403, "insufficient_scope"))
        self.assertEqual(r["anon"].status_code, 401)
        # another person sees nothing of it
        self.fakes.next_user = {**self.fakes.next_user, "sub": "kc-user-2", "preferred_username": "bob"}
        try:
            other = self.access_token()
        finally:
            self.fakes.next_user = {**self.fakes.next_user, "sub": "kc-user-1", "preferred_username": "ada"}

        async def as_other():
            async with httpx2.AsyncClient(timeout=30) as c:
                return (await c.get(f"{base}/api/v1/insights/summary", headers={"Authorization": f"Bearer {other}"})).json()
        theirs = asyncio.run(as_other())
        self.assertEqual((theirs["posts_by_status"], theirs["publish_success"], theirs["ledger_by_status"]), ({}, [], {}))

    def test_openapi_and_docs_pages(self):
        base = self.config.public_url

        async def run():
            async with httpx2.AsyncClient() as c:
                spec = (await c.get(f"{base}/api/openapi.json")).json()
                pages = {p: await c.get(f"{base}{p}") for p in ("/api", "/api/docs", "/api/redoc")}
                assets = {}
                for p in pages.values():
                    for src in re.findall(r"(?:src|href)='(/api/static/[^']+)'", p.text):
                        assets[src] = await c.get(f"{base}{src}")
                etag = next(iter(assets.values())).headers["etag"]
                not_modified = await c.get(f"{base}{next(iter(assets))}", headers={"If-None-Match": etag})
                outside = await c.get(f"{base}/api/static/ASSETS.lock")
                traversal = await c.get(f"{base}/api/static/..%2F..%2Fserver.py")
            return spec, pages, assets, not_modified, outside, traversal
        spec, pages, assets, not_modified, outside, traversal = asyncio.run(run())
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertEqual(set(spec["paths"]), {
            "/api/v1/me", "/api/v1/platforms", "/api/v1/platforms/{platform_id}/connect-link",
            "/api/v1/platforms/{platform_id}/connection", "/api/v1/platforms/{platform_id}/destinations",
            "/api/v1/posts", "/api/v1/posts/{post_id}/cancel", "/api/v1/preview", "/api/v1/stories:sync",
            "/api/v1/ledger", "/api/v1/tokens", "/api/v1/tokens/{token_id}", "/api/v1/insights/summary",
            "/api/v1/insights/kpis"})
        refs = set(re.findall(r'"#/components/schemas/(\w+)"', __import__("json").dumps(spec)))
        self.assertTrue(refs <= set(spec["components"]["schemas"]), refs - set(spec["components"]["schemas"]))
        for path, page in pages.items():
            self.assertEqual(page.status_code, 200, path)
            csp = page.headers["content-security-policy"]
            self.assertIn("script-src 'self';", csp)
            self.assertNotIn("<script>", page.text)            # no inline scripts
            self.assertNotRegex(page.text, r"https?://")        # nothing from a CDN
        self.assertEqual(len(assets), 4)                        # swagger css+js+init, redoc js
        for src, resp in assets.items():
            self.assertEqual(resp.status_code, 200, src)
            self.assertIn("immutable", resp.headers["cache-control"])
        self.assertEqual(not_modified.status_code, 304)
        self.assertEqual(outside.status_code, 404)
        self.assertEqual(traversal.status_code, 404)

    def test_docs_render_in_a_real_browser(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright not installed")
        shots = os.environ.get("INKCAST_CAPTURE_DIR")
        errors = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(f"{self.config.public_url}/api")
            page.wait_for_selector(".swagger-ui .info .title", timeout=15000)
            self.assertIn("Inkcast API", page.inner_text(".swagger-ui .info .title"))
            page.wait_for_selector(".opblock-summary-path[data-path=\"/api/v1/posts\"]", timeout=15000)
            if shots:
                page.screenshot(path=os.path.join(shots, "swagger-ui.png"), full_page=False)
            page.goto(f"{self.config.public_url}/api/redoc")
            page.wait_for_selector("h1:has-text('Inkcast API')", timeout=20000)
            page.locator("h2", has_text="Publish, schedule, draft or preview a story").first.wait_for(timeout=20000)
            if shots:
                page.screenshot(path=os.path.join(shots, "redoc.png"), full_page=False)
            browser.close()
        # The only thing the CSP should ever block is ReDoc's "by Redocly" footer logo, which
        # ReDoc hard-codes to its CDN. Blocking it is the point: the page makes no outside calls.
        csp_errors = [e for e in errors if "Content Security Policy" in e and "cdn.redoc.ly" not in e]
        self.assertEqual(csp_errors, [])

    def test_probes_and_metrics(self):
        async def run():
            async with httpx2.AsyncClient() as c:
                return ((await c.get(f"{self.config.public_url}/livez")).status_code,
                        (await c.get(f"{self.config.public_url}/readyz")).json())
        live, ready = asyncio.run(run())
        self.assertEqual(live, 200)
        self.assertTrue(ready["ok"])
        text = self.app.metrics.render()
        self.assertIn("# TYPE inkcast_publish_total counter", text)
        self.assertIn('inkcast_build_info{platforms="devto,medium",version="0.1.0"} 1', text)
        self.assertIn("inkcast_posts_due", text)

    def test_ambiguous_publish_failure_is_never_retried(self):
        browser = Browser()
        story = STORY.replace("platforms: [medium, devto]", "platforms: [devto]")

        async def connect(out):
            await self.fill_connect_page(out[-1], {"api_key": "devto-key"})

        self.fakes.devto_fail_next = 502
        before = len(self.fakes.devto_articles)
        out = asyncio.run(self.run_tools(self.oauth_client(browser), [
            ("inkcast_connect", {"platform_id": "devto"}), connect, ("inkcast_publish", {"markdown": story})]))
        self.assertIn("devto: UNCONFIRMED", out[-1])
        self.assertIn("may or may not have been published", out[-1])
        self.assertEqual(len(self.fakes.devto_articles), before)
        self.assertGreaterEqual(self.app.store.post_counts().get("unconfirmed", 0), 1)

    def test_denied_consent_returns_access_denied(self):
        browser = Browser(approve=False)

        async def run():
            try:
                await self.run_tools(self.oauth_client(browser), [("inkcast_platforms", {})])
            except Exception:
                pass
        asyncio.run(run())
        self.assertEqual(browser.result.get("error"), "access_denied")

    def test_users_without_required_role_are_refused(self):
        self.fakes.next_user = {**self.fakes.next_user, "sub": "kc-user-2", "realm_access": {"roles": []}}
        try:
            async def run():
                async with httpx2.AsyncClient(follow_redirects=True) as c:
                    reg = (await c.post(f"{self.config.public_url}/register", json={
                        "client_name": "x", "redirect_uris": ["http://localhost:1/cb"], "token_endpoint_auth_method": "none",
                        "grant_types": ["authorization_code"], "response_types": ["code"]})).json()
                    r = await c.get(f"{self.config.public_url}/authorize", params={
                        "client_id": reg["client_id"], "response_type": "code", "redirect_uri": "http://localhost:1/cb",
                        "code_challenge": "x" * 43, "code_challenge_method": "S256", "state": "s"})
                    return r.status_code, r.text
            status, text = asyncio.run(run())
            self.assertEqual(status, 403)
            self.assertIn("inkcast-user", text)
        finally:
            self.fakes.next_user = {**self.fakes.next_user, "sub": "kc-user-1", "realm_access": {"roles": ["inkcast-user"]}}


if __name__ == "__main__":
    unittest.main()
