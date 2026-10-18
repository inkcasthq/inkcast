import asyncio
import time
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


class ConfigTests(unittest.TestCase):
    def test_secrets_from_files(self):
        import os
        import tempfile
        from inkcast.config import load_config
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("s" * 40 + "\n")
        env = {"INKCAST_SECRET_KEY_FILE": f.name, "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            cfg = load_config()
            self.assertEqual(cfg.secret_key, "s" * 40)
            cfg.validate()
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


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


class PipelineRecoveryTests(unittest.TestCase):
    """A post left 'publishing' by a restart is confirmed with the platform or handed to a person, never re-sent blindly."""

    def setUp(self):
        import asyncio
        import httpx2
        from inkcast.libs.db import Store
        from inkcast.libs.pipeline import STUCK_AFTER, Publisher
        from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult

        class Fake(Platform):
            id, name, default_api_url, credential_fields = "fake", "Fake", "https://fake.invalid", []
            capabilities = frozenset({"find"})
            found = None  # what find_existing answers; "error" raises
            sent: list = []

            async def verify(self, creds):
                return Account(id="1", name="fake")

            def prepare(self, fields, body):
                return Prepared(platform="fake", title=fields["title"], payload={"body": body}, preview={})

            async def publish(self, creds, account, prepared, destination):
                self.sent.append(prepared.title)
                return PublishResult(url="https://fake/new", remote_id="new", status="published")

            async def find_existing(self, creds, account, prepared):
                if self.found == "error":
                    raise PlatformError("down")
                return self.found

        self.asyncio, self.STUCK_AFTER, self.PublishResult = asyncio, STUCK_AFTER, PublishResult
        self.store = Store(":memory:", "k" * 40)
        self.store.upsert_user("u", "u", None, None)
        self.fake = Fake(httpx2.AsyncClient())
        self.store.save_credentials("usr", "fake", {"k": "v"}, {"id": "1", "name": "fake"})
        self.publisher = Publisher(self.store, {"fake": self.fake})

    def stuck(self, title="Interrupted"):
        pid = self.store.create_post(group_id="g", user_id="usr", platform="fake", title=title, status="publishing",
                                     run_at=0, payload={"platform": "fake", "title": title, "payload": {}, "preview": {}})
        self.store.update_post(pid)
        self.store._exec("UPDATE posts SET updated_at = ? WHERE id = ?", time.time() - self.STUCK_AFTER - 1, pid)
        return pid

    def test_confirmed_rescheduled_or_unconfirmed_never_a_blind_retry(self):
        found, missing, unknown, errored = self.stuck(), self.stuck(), self.stuck(), self.stuck()
        self.fake.found = self.PublishResult(url="https://fake/it", remote_id="42", status="published")
        self.asyncio.run(self.publisher.run_due())                       # all four are claimed and resolved together
        self.assertEqual(self.store.get_post(found)["status"], "published")   # it had gone out: recorded, not re-sent
        self.assertEqual(self.store.get_post(found)["result_url"], "https://fake/it")
        self.assertEqual(self.fake.sent, [])                                  # nothing was sent during recovery

        for pid, answer in ((missing, None), (unknown, "skip"), (errored, "error")):
            self.store.update_post(pid, status="publishing")
            self.store._exec("UPDATE posts SET updated_at = ? WHERE id = ?", time.time() - self.STUCK_AFTER - 1, pid)
            if answer == "skip":
                self.fake.capabilities = frozenset()    # a platform that can't be asked (Medium)
            else:
                self.fake.capabilities = frozenset({"find"})
                self.fake.found = answer
            with self.assertLogs("inkcast.libs.pipeline", "WARNING") as logs:
                self.asyncio.run(self.publisher.run_due())
            self.assertIn("interrupted mid-send", logs.output[0])
        self.assertEqual(self.store.get_post(missing)["status"], "published")  # missing → re-queued → sent once now
        self.assertEqual(self.fake.sent, ["Interrupted"])
        self.assertEqual(self.store.get_post(unknown)["status"], "unconfirmed")
        self.assertIn("Check Fake before publishing it again", self.store.get_post(unknown)["error"])
        self.assertEqual(self.store.get_post(errored)["status"], "unconfirmed")
        self.assertEqual(self.store.post_counts().get("recovering"), None)
        self.assertEqual(self.store.claim_stuck(time.time()), [])              # a fresh 'publishing' row is left alone


class TokenVerifierTests(unittest.TestCase):
    def test_one_lookup_per_verification_and_pats_get_the_gate_scope(self):
        from inkcast.libs.auth import SCOPE, TokenVerifier
        store = Store(":memory:", "k" * 40)
        user = store.upsert_user("local:owner", "owner", None, None)
        pat, _ = store.create_api_token(user["id"], "ci", ["stories:sync"], None)
        reads = []
        original = store.find_api_token
        store.find_api_token = lambda token: (reads.append(1), original(token))[1]
        v = TokenVerifier(store)
        access = v.access_token(pat)
        self.assertEqual((access.subject, access.scopes, len(reads)), (user["id"], [SCOPE, "stories:sync"], 1))
        self.assertEqual(v.principal(pat).scopes, frozenset({"stories:sync"}))
        self.assertIsNone(v.access_token("ink_pat_nope_nope"))
        self.assertIsNone(v.access_token(""))


class CliSafetyTests(unittest.TestCase):
    def test_token_is_never_sent_over_plain_http_to_a_remote_host(self):
        import contextlib
        import io
        import os
        from inkcast.cli import sync_main
        os.environ["INKCAST_TOKEN_TEST"] = "ink_pat_x_y"
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = sync_main(["--url", "http://inkcast.example.com", "--token-env", "INKCAST_TOKEN_TEST", "nothing/*.md"])
        finally:
            del os.environ["INKCAST_TOKEN_TEST"]
        self.assertEqual(code, 2)
        self.assertIn("plain http", err.getvalue())
