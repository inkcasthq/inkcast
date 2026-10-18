import asyncio
import unittest
from datetime import datetime, timezone

import httpx2

from inkcast.auth.provider import InkcastOAuthProvider
from inkcast.config import Config
from inkcast.platforms import PlatformError, discover
from inkcast.platforms.devto import DevtoPlatform
from inkcast.platforms.medium import MediumPlatform
from inkcast.storage import Store
from inkcast.story import StoryError, parse_story

NOW = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)
KNOWN = ["medium", "devto"]
FILE = """---
title: Blog title
description: The summary
tags: [AI, Machine Learning]
canonical_url: https://blog.example.com/post
medium:
  title: Punchier Medium title
  publish_status: unlisted
devto:
  series: Agents
  tags: [ai, ml]
---

# Blog title

![arch](./img/arch.png)

Body.
"""


class StoryTests(unittest.TestCase):
    def test_front_matter_and_overrides(self):
        s = parse_story(FILE, known_platforms=KNOWN, now=NOW)
        self.assertEqual((s.title, s.subtitle), ("Blog title", "The summary"))
        self.assertNotIn("# Blog title", s.body)  # title is presented by each platform
        self.assertEqual(s.for_platform("medium")["title"], "Punchier Medium title")
        self.assertEqual(s.for_platform("devto")["tags"], ["ai", "ml"])
        self.assertTrue(any("./img/arch.png" in w for w in s.warnings))

    def test_image_base_url(self):
        s = parse_story(FILE, known_platforms=KNOWN, image_base_url="https://blog.example.com/post/", now=NOW)
        self.assertIn("https://blog.example.com/post/img/arch.png", s.body)

    def test_schedule_validation(self):
        s = parse_story(FILE, known_platforms=KNOWN, publish_at="2026-10-21T09:00:00-05:00", now=NOW)
        self.assertEqual(s.publish_at.isoformat(), "2026-10-21T14:00:00+00:00")
        with self.assertRaisesRegex(StoryError, "time zone"):
            parse_story(FILE, known_platforms=KNOWN, publish_at="2026-10-21T09:00:00", now=NOW)
        with self.assertRaisesRegex(StoryError, "past"):
            parse_story(FILE, known_platforms=KNOWN, publish_at="2026-01-01T00:00:00Z", now=NOW)

    def test_errors(self):
        with self.assertRaisesRegex(StoryError, "title"):
            parse_story("just text", known_platforms=KNOWN, now=NOW)
        with self.assertRaisesRegex(StoryError, "Unknown platform"):
            parse_story(FILE, known_platforms=KNOWN, platforms=["myspace"], now=NOW)
        with self.assertRaisesRegex(StoryError, "canonical_url"):
            parse_story(FILE, known_platforms=KNOWN, canonical_url="blog/post", now=NOW)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.http = httpx2.AsyncClient()
        self.story = parse_story(FILE, known_platforms=KNOWN, now=NOW)

    def test_medium_prepare(self):
        p = MediumPlatform(self.http).prepare(self.story.for_platform("medium"), self.story.body)
        self.assertTrue(p.payload["content"].startswith("# Punchier Medium title\n\n## The summary\n\n"))
        self.assertEqual(p.payload["publishStatus"], "unlisted")
        self.assertEqual(p.payload["canonicalUrl"], "https://blog.example.com/post")
        with self.assertRaises(PlatformError):
            MediumPlatform(self.http).prepare({**self.story.for_platform("medium"), "title": "x" * 101}, "b")

    def test_devto_prepare(self):
        fields = {**self.story.for_platform("devto"), "tags": ["AI", "Machine Learning", "c++", "go", "rust"]}
        p = DevtoPlatform(self.http).prepare(fields, self.story.body)
        a = p.payload["article"]
        self.assertEqual(a["tags"], ["ai", "machinelearning", "c", "go"])
        self.assertEqual((a["description"], a["series"]), ("The summary", "Agents"))
        self.assertTrue(any("lowercase" in w for w in p.warnings))

    def test_registry_has_builtins(self):
        self.assertEqual(set(discover()), {"medium", "devto"})


class OAuthTests(unittest.TestCase):
    def test_refresh_rotation_and_single_use_codes(self):
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:",
                     login="password", password="p" * 12)
        store = Store(cfg.database_path, cfg.secret_key)
        oauth = InkcastOAuthProvider(cfg, store)
        user = store.upsert_user("local:owner", "owner", None, None)

        async def run():
            from mcp.server.auth.provider import AuthorizationParams, TokenError
            from mcp.shared.auth import OAuthClientInformationFull
            client = OAuthClientInformationFull(client_id="c1", redirect_uris=["http://localhost:1/cb"],
                                                token_endpoint_auth_method="none")
            await oauth.register_client(client)
            params = AuthorizationParams(state="s", scopes=["inkcast"], code_challenge="x" * 43,
                                         redirect_uri="http://localhost:1/cb", redirect_uri_provided_explicitly=True)
            url = await oauth.authorize(client, params)
            pending = store.get_pending(url.split("pending=")[1])
            store.set_pending_user(pending["id"], user["id"])
            redirect = oauth.approve(store.get_pending(pending["id"]))
            code = redirect.split("code=")[1].split("&")[0]
            ac = await oauth.load_authorization_code(client, code)
            tokens = await oauth.exchange_authorization_code(client, ac)
            with self.assertRaises(TokenError):
                await oauth.exchange_authorization_code(client, ac)
            self.assertEqual((await oauth.load_access_token(tokens.access_token)).subject, user["id"])
            rt = await oauth.load_refresh_token(client, tokens.refresh_token)
            new = await oauth.exchange_refresh_token(client, rt, [])
            self.assertIsNone(await oauth.load_access_token(tokens.access_token))
            self.assertIsNone(await oauth.load_refresh_token(client, tokens.refresh_token))
            self.assertIsNotNone(await oauth.load_access_token(new.access_token))
            # Tokens are stored hashed, credentials encrypted.
            raw = store._all("SELECT token_hash FROM tokens")
            self.assertTrue(all(new.access_token not in r["token_hash"] for r in raw))
            store.save_credentials(user["id"], "devto", {"api_key": "secret-key"}, {"id": "1", "name": "x"})
            sealed = store._one("SELECT sealed FROM credentials")["sealed"]
            self.assertNotIn("secret-key", sealed)
        asyncio.run(run())


class GitHubIdentityTests(unittest.TestCase):
    def _finish(self, orgs, allowed_orgs=(), allowed_users=()):
        from starlette.requests import Request
        from inkcast.auth.identity import GitHubIdentity

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/login/oauth/access_token":
                return httpx2.Response(200, json={"access_token": "gho_x"})
            if request.url.path == "/user":
                return httpx2.Response(200, json={"id": 99, "login": "Ada", "name": "Ada L", "email": None})
            if request.url.path == "/user/orgs":
                return httpx2.Response(200, json=[{"login": o} for o in orgs])
            return httpx2.Response(404)

        cfg = Config(public_url="https://inkcast.example.com", secret_key="k" * 40, database_path=":memory:",
                     login="github", github_client_id="id", github_client_secret="sec",
                     github_allowed_orgs=allowed_orgs, github_allowed_users=allowed_users)
        idp = GitHubIdentity(cfg, httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        req = Request({"type": "http", "method": "GET", "path": "/auth/callback", "headers": [],
                       "query_string": b"code=abc&state=pend1"})
        return asyncio.run(idp.finish(req))

    def test_org_member_allowed(self):
        pending, who = self._finish(["Analytical"], allowed_orgs=("analytical",))
        self.assertEqual((pending, who.key, who.display), ("pend1", "github:99", "@Ada"))

    def test_outsider_refused(self):
        from inkcast.auth.identity import LoginError
        with self.assertRaises(LoginError):
            self._finish(["other"], allowed_orgs=("analytical",))
        pending, who = self._finish([], allowed_users=("ada",))
        self.assertEqual(who.key, "github:99")


if __name__ == "__main__":
    unittest.main()
