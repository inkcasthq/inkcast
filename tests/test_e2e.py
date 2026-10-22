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

    extra_config: dict = {}   # settings a subclass adds (e.g. the Keycloak user sync)

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
            **cls.extra_config,
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

    def test_the_keycloak_sign_in_driver(self):
        """scripts/dev/oidc_signin.py, which CI runs against a real Keycloak (E9.F9.1), here against the fake identity
        provider showing Keycloak's login form: a person with inkcast-user signs in, one without is refused by
        Inkcast, and a wrong password stops at Keycloak. Runs in single and split mode."""
        import pathlib
        import subprocess
        import sys
        person = {"email_verified": True, "name": "Ada Example"}
        self.fakes.kc_accounts = {
            "ada@example.com": ("right-password-123", {**person, "sub": "kc-ada", "preferred_username": "ada@example.com",
                                                       "email": "ada@example.com", "realm_access": {"roles": ["inkcast-user"]}}),
            "no-role@example.com": ("right-password-456", {**person, "sub": "kc-norole", "email": "no-role@example.com",
                                                           "preferred_username": "no-role@example.com",
                                                           "realm_access": {"roles": []}}),
        }
        self.addCleanup(setattr, self.fakes, "kc_accounts", None)
        script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "dev" / "oidc_signin.py"

        def drive(user: str, password: str, expect: str, register: bool = False) -> subprocess.CompletedProcess:
            env = {**os.environ, "KC_USER": user, "KC_PASSWORD": password, "KC_EXPECT": expect,
                   "KC_REGISTER": "1" if register else "", "MAILPIT_URL": f"{self.fakes.base}/mailpit"}
            return subprocess.run([sys.executable, str(script), self.config.public_url], capture_output=True, text=True,
                                  timeout=90, env=env)

        ok = drive("ada@example.com", "right-password-123", "allowed")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("PASS: signed in through Keycloak", ok.stdout)
        self.assertIn("ada@example.com", ok.stdout)
        refused = drive("no-role@example.com", "right-password-456", "refused")
        self.assertEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("PASS: refused as expected", refused.stdout)
        wrong = drive("ada@example.com", "not-the-password", "allowed")
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("Keycloak did not sign the user in", wrong.stderr)
        # The driver must not pass on a refusal it didn't expect.
        surprised = drive("no-role@example.com", "right-password-456", "allowed")
        self.assertNotEqual(surprised.returncode, 0)
        # Self sign-up (E9.F9.2.S1): register on Keycloak's page, verify the emailed link, accept the terms, and
        # arrive in Inkcast with the default role; a person who never verifies never reaches Inkcast.
        newcomer = f"new-{self.id().rsplit('.', 2)[-2].lower()}@example.com"
        joined = drive(newcomer, "a-good-long-password-1", "allowed", register=True)
        self.assertEqual(joined.returncode, 0, joined.stdout + joined.stderr)
        self.assertIn("verification email received", joined.stdout)
        self.assertIn("terms shown; accepting", joined.stdout)
        self.assertIn("PASS: signed in through Keycloak after signing up", joined.stdout)
        held = drive(f"unverified-{newcomer}", "a-good-long-password-2", "unverified", register=True)
        self.assertEqual(held.returncode, 0, held.stdout + held.stderr)
        self.assertIn("PASS: unverified held back", held.stdout)

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
                long_hourly = await c.get(f"{base}/api/v1/insights/kpis?names=posts_published&grain=hour&days=60",
                                          headers=h)
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
        self.assertEqual(r["long_hourly"].status_code, 422)               # hourly is capped at 31 days
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
            "/api/v1/ledger", "/api/v1/tokens", "/api/v1/tokens/{token_id}", "/api/v1/account/deletion-link",
            "/api/v1/insights/summary", "/api/v1/insights/kpis"})
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
            # #60: light only. ReDoc paints its right-hand panel dark unless themed; no large dark area may remain,
            # at the top or beside an operation's code samples.
            dark_areas = """() => {
                const lum = c => { const m = c.match(/[\\d.]+/g); if (!m || (m.length > 3 && +m[3] === 0)) return null;
                    const [r, g, b] = m.slice(0, 3).map(Number); return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255; };
                const area = innerWidth * innerHeight, out = [];
                for (const el of document.querySelectorAll('body *')) {
                    const r = el.getBoundingClientRect(), l = lum(getComputedStyle(el).backgroundColor);
                    if (l !== null && l < 0.35 && r.width * r.height > area * 0.05) out.push(el.tagName + ' ' + l.toFixed(2));
                }
                return out; }"""
            self.assertEqual(page.evaluate(dark_areas), [])
            page.goto(f"{self.config.public_url}/api/redoc#tag/Posts/operation/publish")
            page.get_by_text("Request samples").first.wait_for(timeout=20000)
            self.assertEqual(page.evaluate(dark_areas), [])
            key = page.locator(".redoc-json .token.property.string").first
            self.assertNotEqual(key.evaluate("e => getComputedStyle(e).color"), "rgb(255, 255, 255)", "white keys on light")
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


    def test_keycloak_disabling_or_deleting_a_person_cuts_off_their_tokens(self):
        """E9.F9.5.S2: a person Keycloak disables loses their OAuth token and personal access tokens on the sync's
        next pass (admin events), one deleted while no event was seen is caught by the reconcile, and signing in
        again once Keycloak allows it gives access back."""
        import dataclasses
        from inkcast.libs.secrets import SecretValue
        from inkcast.services.identity.keycloak_sync import KeycloakSync

        base, first = self.config.public_url, dict(self.fakes.next_user)
        self.addCleanup(setattr, self.fakes, "next_user", first)
        people = {}
        for sub in ("kc-sync-ada", "kc-sync-bob"):
            self.fakes.next_user = {**first, "sub": sub, "preferred_username": sub, "email": f"{sub}@example.com"}
            token = self.access_token()

            async def pat(token=token):
                async with httpx2.AsyncClient() as c:
                    r = await c.post(f"{base}/api/v1/tokens", headers={"Authorization": f"Bearer {token}"},
                                     json={"name": "sync", "scopes": ["posts:read"]})
                    return r.raise_for_status().json()["token"]
            people[sub] = (token, asyncio.run(pat()))

        async def status(token):
            async with httpx2.AsyncClient() as c:
                return (await c.get(f"{base}/api/v1/me", headers={"Authorization": f"Bearer {token}"})).status_code

        def statuses(sub):
            return [asyncio.run(status(t)) for t in people[sub]]

        self.assertEqual(statuses("kc-sync-ada") + statuses("kc-sync-bob"), [200] * 4)
        config = dataclasses.replace(self.config, keycloak_sync_client_id="inkcast-sync",
                                     keycloak_sync_secret=SecretValue("sync-secret"))

        async def tick(sync):
            async with httpx2.AsyncClient(timeout=10) as http:
                sync.http = http
                return await sync.tick()

        sync = KeycloakSync(config, self.app.store, None, self.app.metrics)
        self.assertEqual(asyncio.run(tick(sync))["disabled"], 0, "start: everyone enabled, nothing cut off")

        self.fakes.kc_admin_change("kc-sync-ada", enabled=False)               # an admin disables Ada
        self.fakes._kc_sync_tokens.clear()                                      # and its token went stale: retried
        self.assertEqual(asyncio.run(tick(sync)), {"checked": 1, "disabled": 1, "erased": 0})
        self.assertEqual(statuses("kc-sync-ada"), [401, 401], "OAuth token and PAT cut off")
        self.assertEqual(statuses("kc-sync-bob"), [200, 200], "nobody else touched")

        self.fakes.kc_deleted.add("kc-sync-bob")                                # deleted, no admin event seen
        self.assertEqual(asyncio.run(tick(sync))["disabled"], 0, "the reconcile is not due yet")
        sync.last_reconcile = 0
        self.assertEqual(asyncio.run(tick(sync))["disabled"], 1)
        self.assertEqual(statuses("kc-sync-bob"), [401, 401])
        self.assertIn('inkcast_identity_users_disabled_total{reason="deleted"} 1', self.app.metrics.render())

        self.fakes.kc_admin_change("kc-sync-ada", enabled=True)                 # re-enabled: sign in again
        self.fakes.next_user = {**first, "sub": "kc-sync-ada", "preferred_username": "kc-sync-ada",
                                "email": "kc-sync-ada@example.com"}
        self.assertEqual(asyncio.run(status(self.access_token())), 200)
        self.assertEqual(statuses("kc-sync-ada"), [401, 401], "the old tokens stay dead")
        self.assertEqual(asyncio.run(tick(sync))["disabled"], 0, "an enabled person is left alone")

        self.fakes.kc_sync_client = ("inkcast-sync", "rotated")                 # a wrong secret: an error, not a crash
        self.fakes._kc_sync_tokens.clear()
        sync._token = None
        with self.assertRaises(Exception):
            asyncio.run(tick(sync))
        self.fakes.kc_sync_client = ("inkcast-sync", "sync-secret")

class AccountDeletion(Harness):
    """E9.F9.5.S4 (issue #41): Delete my account. Inkcast hands out a one-time link; the person signs in again, reads
    what goes and what stays, continues to Keycloak's delete_account action, confirms there, and comes back through
    Keycloak's "deleted" page; Inkcast confirms the person is gone through the sync's read-only view-users and only
    then erases. Cancel and errors erase nothing; a forged or replayed return visit erases nothing; the sync's
    DELETE_ACCOUNT reader erases anyone the return visit never confirmed (the account console too)."""

    extra_config = {"keycloak_sync_client_id": "inkcast-sync", "keycloak_sync_secret": "sync-secret"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fakes.kc_base_url = f"{cls.config.public_url}/account/deleted"   # the inkcast-identity client's baseUrl

    def setUp(self):
        from inkcast.libs.db.erasure import USER_TABLES
        self.USER_TABLES = USER_TABLES
        self.base, self.first = self.config.public_url, dict(self.fakes.next_user)
        self.addCleanup(setattr, self.fakes, "next_user", self.first)
        self.addCleanup(setattr, self.fakes, "kc_delete_fails", False)

    def person(self, sub: str) -> dict:
        """Signs ``sub`` in (an OAuth token), gives them a PAT, a connected platform, a scheduled post and a ledger
        entry; returns what a test needs to check them afterwards."""
        self.fakes.next_user = {**self.first, "sub": sub, "preferred_username": sub, "email": f"{sub}@example.com"}
        token = self.access_token()
        user = self.app.store.find_user_by_identity(f"oidc:{self.fakes.issuer}#{sub}")
        store = self.app.store
        store.save_credentials(user["id"], "devto", {"api_key": "devto-key"}, {"id": "1", "name": sub})
        store.create_post(group_id="g", user_id=user["id"], platform="devto", title="Later", status="scheduled",
                          run_at=time.time() + 3600, payload={"platform": "devto", "title": "Later", "payload": {}, "preview": {}})
        store.put_entry(user["id"], "repo", "posts/a.md", "devto", content_hash="h", status="published")
        pat = self.call("POST", "/api/v1/tokens", token, json={"name": "ci", "scopes": ["posts:read"]}).json()["token"]
        return {"sub": sub, "id": user["id"], "oauth": token, "pat": pat}

    def call(self, method: str, path: str, token: str, **kw) -> httpx2.Response:
        async def go():
            async with httpx2.AsyncClient(timeout=20) as c:
                return await c.request(method, f"{self.base}{path}", headers={"Authorization": f"Bearer {token}"}, **kw)
        return asyncio.run(go())

    def statuses(self, who: dict) -> list[int]:
        return [self.call("GET", "/api/v1/me", t).status_code for t in (who["oauth"], who["pat"])]

    def rows(self, user_id: str) -> dict[str, int]:
        store = self.app.store
        with store._lock:
            return {t: int(store._conn.execute(f"SELECT COUNT(*) AS n FROM {t} WHERE {c}", {"user_id": user_id}).fetchone()["n"])
                    for t, c in self.USER_TABLES}

    def link(self, who: dict) -> str:
        """The one-time link, from the MCP tool (the REST endpoint gives the same)."""
        self.fakes.next_user = {**self.first, "sub": who["sub"], "preferred_username": who["sub"], "email": f"{who['sub']}@example.com"}
        storage = Memory()
        out = asyncio.run(self.run_tools(self.oauth_client(Browser(), storage), [("inkcast_delete_account", {})]))
        self.assertIn("Nothing has been deleted yet", out[0])
        return re.search(rf"({self.base}/account/delete/\S+)", out[0]).group(1)

    async def to_confirm_page(self, c: httpx2.AsyncClient, link: str) -> httpx2.Response:
        """Open the link: a fresh sign-in at (the fake) Keycloak, then Inkcast's confirm page."""
        r = await c.get(link)
        for _ in range(8):
            if not (r.status_code in (302, 303)) or "/auth/consent" in r.headers["location"]:
                break
            r = await c.get(urljoin(str(r.url), r.headers["location"]))
        self.assertIn("/auth/consent", r.headers.get("location", ""), r.text[:300])
        return await c.get(urljoin(str(r.url), r.headers["location"]))

    async def continue_to_keycloak(self, c: httpx2.AsyncClient, page: httpx2.Response) -> httpx2.Response:
        """Press Continue: Inkcast sets the return cookie and sends the browser to Keycloak's delete_account action,
        which (after signing in again) shows its confirm page."""
        fields = dict(re.findall(r'name=(\w+) value="([^"]*)"', page.text))
        r = await c.post(f"{self.base}/auth/consent", data={"pending": fields["pending"], "csrf": fields["csrf"], "decision": "approve"})
        self.assertEqual(r.status_code, 302, r.text[:300])
        self.assertIn("kc_action=delete_account", r.headers["location"])
        self.assertIn("code_challenge=", r.headers["location"])
        self.assertRegex(r.headers.get("set-cookie", ""), r"inkcast_deletion=[^;]+;.*HttpOnly.*SameSite=lax|inkcast_deletion=[^;]+;.*SameSite=lax.*HttpOnly")
        self.assertIn("Path=/account", r.headers["set-cookie"])
        confirm = await c.get(r.headers["location"])
        self.assertEqual(confirm.status_code, 200)
        self.assertIn("Delete account confirmation", confirm.text)
        return confirm

    @staticmethod
    def keycloak_form(confirm: httpx2.Response) -> str:
        import html
        return html.unescape(re.search(r'<form action="([^"]+)"', confirm.text).group(1))

    def test_the_person_deletes_their_account_and_comes_back_through_keycloaks_deleted_page(self):
        ada, bob = self.person("kc-del-ada"), self.person("kc-del-bob")
        before_bob = self.rows(bob["id"])
        self.assertEqual(self.statuses(ada) + self.statuses(bob), [200] * 4)
        self.assertEqual(self.call("POST", "/api/v1/account/deletion-link", ada["pat"]).status_code, 403, "a PAT is refused")
        rest = self.call("POST", "/api/v1/account/deletion-link", ada["oauth"]).json()
        self.assertTrue(rest["url"].startswith(f"{self.base}/account/delete/") and rest["expires_in"] == 900, rest)
        link = self.link(ada)
        erasures = len(self.app.store.user_erasures())

        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                page = await self.to_confirm_page(c, link)
                reused = await c.get(link)
                confirm = await self.continue_to_keycloak(c, page)
                deleted = await c.post(self.keycloak_form(confirm), data={})      # "Delete Account" at Keycloak
                back = re.search(r'href="([^"]+)"[^>]*>[^<]*Back to Application', deleted.text).group(1)
                home = await c.get(back)                                          # Keycloak's page links to Inkcast
                again = await c.get(back)
            return page, reused, deleted, home, again
        page, reused, deleted, home, again = asyncio.run(run())
        self.assertIn("Delete your Inkcast account?", page.text)
        for said in ("posts and schedules", "ledger", "credentials stored", "personal\naccess tokens", "usage numbers",
                     "articles already published on the platforms", "coming soon"):
            self.assertIn(said, page.text, said)
        self.assertEqual(reused.status_code, 400, "the link is one use")
        self.assertIn("User deleted successfully", deleted.text)
        self.assertEqual(home.status_code, 200)
        self.assertIn("Your account is deleted and your data on Inkcast is erased", home.text)
        self.assertRegex(home.headers["set-cookie"], r'inkcast_deletion=""?;.*(Max-Age=0|expires=)', "the cookie is cleared")
        self.assertIn("being finished", again.text, "a replay of the visit: the record is gone, nothing to erase")

        self.assertEqual(self.statuses(ada), [401, 401], "her tokens answer 401")
        self.assertEqual(set(self.rows(ada["id"]).values()), {0}, "her rows are gone")
        self.assertIn("kc-del-ada", self.fakes.kc_deleted, "her Keycloak user is gone")
        self.assertEqual(self.statuses(bob), [200, 200])
        self.assertEqual(self.rows(bob["id"]), before_bob, "bob is untouched")
        self.assertIn('inkcast_identity_users_deleted_total{via="return"} 1', self.app.metrics.render())
        self.assertEqual(len(self.app.store.user_erasures()), erasures + 1, "one audit record")
        self.assertEqual(self.call("GET", "/api/v1/me", ada["oauth"]).status_code, 401)

        # The backstop sees her DELETE_ACCOUNT event too, and has nothing left to erase.
        self.assertEqual(self.sync_tick()["erased"], 0)
        self.assertEqual(self.statuses(bob), [200, 200])

    def sync_tick(self) -> dict:
        from inkcast.services.identity.keycloak_sync import KeycloakSync
        sync = self.app.extras.get("keycloak_sync") or KeycloakSync(self.config, self.app.store, None, self.app.metrics)

        async def tick():
            async with httpx2.AsyncClient(timeout=10) as http:
                sync.http = http
                try:
                    return await sync.tick()
                finally:
                    sync.http = self.app.http
        return asyncio.run(tick())

    def test_cancel_at_keycloak_or_an_error_there_deletes_nothing(self):
        ada = self.person("kc-del-cancel")

        async def run(link: str, cancel: bool):
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                page = await self.to_confirm_page(c, link)
                confirm = await self.continue_to_keycloak(c, page)
                r = await c.post(self.keycloak_form(confirm), data={"cancel-aia": "true"} if cancel else {})
                self.assertEqual(r.status_code, 302)
                self.assertTrue(r.headers["location"].startswith(f"{self.base}/auth/callback?"), r.headers["location"])
                query = parse_qs(urlparse(r.headers["location"]).query)
                back = await c.get(r.headers["location"])
                stale = await c.get(f"{self.base}/account/deleted")
            return query, back, stale
        link = self.link(ada)
        before = self.rows(ada["id"])
        query, back, stale = asyncio.run(run(link, cancel=True))
        self.assertEqual(query["kc_action_status"], ["cancelled"])
        self.assertIn("Nothing was deleted", back.text)
        self.assertIn("You cancelled", back.text)
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(self.rows(ada["id"]), {**before, "deletion_requests": 0}, "nothing erased on cancel; the request is over")
        self.assertEqual(self.statuses(ada), [200, 200])

        self.fakes.kc_delete_fails = True
        link = self.link(ada)
        before = self.rows(ada["id"])
        query, back, stale = asyncio.run(run(link, cancel=False))
        self.assertEqual(query["error"], ["access_denied"])
        self.assertIn("Nothing was deleted", back.text)
        self.assertIn("could not delete your account", back.text)
        self.assertEqual(self.rows(ada["id"]), {**before, "deletion_requests": 0}, "nothing erased on an error")
        self.assertEqual(self.statuses(ada), [200, 200])
        self.assertNotIn("kc-del-cancel", self.fakes.kc_deleted)
        self.assertNotIn('via="return"} 2', self.app.metrics.render())

    def test_a_forged_or_replayed_return_visit_erases_nothing(self):
        ada = self.person("kc-del-forged")
        link = self.link(ada)
        before = self.rows(ada["id"])

        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                page = await self.to_confirm_page(c, link)
                armed = await self.continue_to_keycloak(c, page)             # the cookie is set; Keycloak still has her
                not_yet = await c.get(f"{self.base}/account/deleted")        # a visit before Keycloak deleted anything
                async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as other:
                    no_cookie = await other.get(f"{self.base}/account/deleted")
                    forged = await other.get(f"{self.base}/account/deleted", cookies={"inkcast_deletion": "x" * 43})
                self.assertIn("Delete account confirmation", armed.text)
            return not_yet, no_cookie, forged
        not_yet, no_cookie, forged = asyncio.run(run())
        for r in (not_yet, no_cookie, forged):
            self.assertEqual(r.status_code, 200)
            self.assertIn("being finished", r.text)
        self.assertEqual(self.rows(ada["id"]), before, "nothing erased without Keycloak's 404")
        self.assertEqual(self.statuses(ada), [200, 200])
        self.assertNotIn("kc-del-forged", self.fakes.kc_deleted)

    def test_a_link_opened_by_another_person_is_refused(self):
        ada, bob = self.person("kc-del-link-ada"), self.person("kc-del-link-bob")
        link = self.link(ada)
        self.fakes.next_user = {**self.first, "sub": "kc-del-link-bob", "preferred_username": "bob"}   # bob opens it

        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                return await self.to_confirm_page(c, link)
        page = asyncio.run(run())
        self.assertEqual(page.status_code, 403)
        self.assertIn("belongs to another account", page.text)
        self.assertEqual(self.statuses(ada) + self.statuses(bob), [200] * 4)

    def test_a_deletion_from_the_account_console_is_erased_by_the_sync_within_a_pass(self):
        carol, dave = self.person("kc-del-carol"), self.person("kc-del-dave")
        before_dave = self.rows(dave["id"])
        self.assertEqual(self.sync_tick()["erased"], 0)
        self.fakes.kc_account_console_delete("kc-del-carol")                 # Keycloak's account console: Delete account
        self.assertEqual(self.statuses(carol), [200, 200], "nothing happened yet: Inkcast has not read the event")
        self.assertEqual(self.sync_tick()["erased"], 1)
        self.assertEqual(self.statuses(carol), [401, 401])
        self.assertEqual(set(self.rows(carol["id"]).values()), {0})
        self.assertEqual(self.rows(dave["id"]), before_dave)
        self.assertEqual(self.statuses(dave), [200, 200])
        self.assertIn('inkcast_identity_users_deleted_total{via="event"} 1', self.app.metrics.render())
        self.assertEqual(self.sync_tick()["erased"], 0, "the cursor moved: the event is not replayed")
        self.assertIn("/kc/admin/realms/inkcast/events", self.fakes.kc_admin_calls)

    def test_without_the_sync_there_is_no_link(self):
        import dataclasses
        from inkcast.libs.account import deletion_link
        from inkcast.libs.http.rest import ApiError
        ada = self.person("kc-del-nosync")
        for config in (dataclasses.replace(self.config, keycloak_sync_client_id=""),
                       dataclasses.replace(self.config, login="password", password="p" * 12)):
            with self.assertRaises(ApiError) as refused:
                deletion_link(config, self.app.store, ada["id"])
            self.assertEqual(refused.exception.status, 409)
            self.assertIn("not available", refused.exception.message.lower())
        self.assertEqual(self.rows(ada["id"])["deletion_requests"], 0, "no request row without a link")


if __name__ == "__main__":
    unittest.main()
