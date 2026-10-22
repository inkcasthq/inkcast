# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The scheduling horizon (#95).

`publish_at` had no upper bound while every listing capped `days_ahead` at 730 days, so a post scheduled further out
was accepted but could never be listed or cancelled from MCP or REST. One constant, SCHEDULE_HORIZON_DAYS, is now both
the latest `publish_at` and the listing cap: exactly at the horizon is accepted and listable; one second beyond is
refused on every entry point (inkcast_publish, inkcast_sync, REST publish, REST sync) with an error that names the
limit and the latest allowed time."""

import asyncio
import pathlib
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.content import story as content
from inkcast.libs.contracts.openapi import openapi_document
from inkcast.libs.http.app import compose
from test_e2e import free_port, serve

HORIZON_DAYS = 730                                          # two years: the listing cap the issue names
NOW = datetime.now(timezone.utc).replace(microsecond=0)     # the story parser's clock in every test below
AT_HORIZON = NOW + timedelta(days=HORIZON_DAYS)
BEYOND = AT_HORIZON + timedelta(seconds=1)
KNOWN = ["medium", "devto"]


class FrozenClock(datetime):
    """`datetime` as the story parser sees it: `now()` is NOW, so "one second beyond" is exact, not a race."""

    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def frozen():
    return mock.patch.object(content, "datetime", FrozenClock)


def story_md(publish_at: datetime) -> str:
    return f"---\ntitle: Far ahead\nplatforms: [devto]\npublish_at: {publish_at.isoformat()}\n---\n\nBody.\n"


def assert_names_the_limit(test: unittest.TestCase, message: str) -> None:
    test.assertIn(f"{HORIZON_DAYS} days", message)
    test.assertIn(AT_HORIZON.isoformat(), message)


class Parser(unittest.TestCase):
    """libs/content/story: the maximum lead time lives next to the two-minute minimum."""

    def test_the_constant_is_the_two_year_horizon(self):
        self.assertEqual(content.SCHEDULE_HORIZON_DAYS, HORIZON_DAYS)

    def test_exactly_at_the_horizon_is_accepted(self):
        by_argument = content.parse_story("# Far ahead\n\nBody.\n", known_platforms=KNOWN,
                                          publish_at=AT_HORIZON.isoformat(), now=NOW)
        by_front_matter = content.parse_story(story_md(AT_HORIZON), known_platforms=KNOWN, now=NOW)
        self.assertEqual((by_argument.publish_at, by_front_matter.publish_at), (AT_HORIZON, AT_HORIZON))

    def test_one_second_beyond_is_refused_naming_the_limit_and_the_latest_allowed_time(self):
        with self.assertRaises(content.StoryError) as caught:
            content.parse_story("# Far ahead\n\nBody.\n", known_platforms=KNOWN, publish_at=BEYOND.isoformat(), now=NOW)
        assert_names_the_limit(self, str(caught.exception))
        # Git sync's leniency (a past date means "now") is for the past only.
        with self.assertRaises(content.StoryError) as caught:
            content.parse_story(story_md(BEYOND), known_platforms=KNOWN, now=NOW, past_schedule_is_now=True)
        assert_names_the_limit(self, str(caught.exception))


class EntryPoints(unittest.TestCase):
    """PublishingApi over real HTTP: one user with DEV connected and a personal access token for every scope."""

    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.config = Config(public_url=f"http://127.0.0.1:{port}", port=port, secret_key="k" * 48,
                            database_path=":memory:", login="password", password="p" * 12)
        specs = resolve("PublishingApi")
        cls.ctx = build(cls.config, specs)
        cls.server = serve(compose(specs, cls.ctx), port)
        store = cls.ctx.store
        cls.user = store.upsert_user("local:owner", "owner", None, None)["id"]
        store.save_credentials(cls.user, "devto", {"api_key": "devto-key"}, {"id": "1", "name": "owner", "handle": "owner"})
        cls.token, _ = store.create_api_token(cls.user, "test", ["posts:read", "posts:write", "stories:sync"], None)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def rest(self, method: str, path: str, **kw) -> httpx2.Response:
        async def run():
            async with httpx2.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.token}"}) as c:
                return await c.request(method, f"{self.config.public_url}{path}", **kw)
        return asyncio.run(run())

    def tool(self, name: str, args: dict) -> tuple[bool, str]:
        """(is_error, text) of one MCP tool call, signed in with the personal access token."""
        async def run():
            async with httpx2.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.token}"}) as http:
                async with streamable_http_client(self.config.mcp_url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        r = await session.call_tool(name, args)
            return r.is_error, "\n".join(c.text for c in r.content if hasattr(c, "text"))
        return asyncio.run(run())

    def test_rest_accepts_the_horizon_lists_it_and_refuses_one_second_beyond(self):
        with frozen():
            accepted = self.rest("POST", "/api/v1/posts", json={"markdown": story_md(AT_HORIZON)})
            refused = self.rest("POST", "/api/v1/posts", json={"markdown": story_md(BEYOND)})
            preview = self.rest("POST", "/api/v1/preview", json={"markdown": story_md(BEYOND)})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        [result] = accepted.json()["results"]
        self.assertEqual(result["status"], "scheduled")
        for r in (refused, preview):
            self.assertEqual(r.status_code, 422, r.text)
            self.assertEqual(r.json()["error"], "invalid_story")
            assert_names_the_limit(self, r.json()["message"])
        # The listing's cap is the horizon, so the post is there to find and cancel; one day more is refused.
        listed = self.rest("GET", f"/api/v1/posts?days_ahead={HORIZON_DAYS}")
        self.assertIn(result["post_id"], {p["id"] for p in listed.json()}, listed.text)
        self.assertEqual(self.rest("GET", f"/api/v1/posts?days_ahead={HORIZON_DAYS + 1}").status_code, 422)
        self.assertEqual(self.rest("POST", f"/api/v1/posts/{result['post_id']}/cancel").json()["status"], "cancelled")

    def test_rest_sync_reports_one_second_beyond_as_failed(self):
        def sync(when):
            return self.rest("POST", "/api/v1/stories:sync", json={
                "source": self.id(), "files": [{"path": "posts/far.md", "markdown": story_md(when)}]})
        with frozen():
            accepted = sync(AT_HORIZON)
            refused = sync(BEYOND)
        [ok] = accepted.json()["results"]
        self.assertEqual((ok["action"], ok["status"]), ("create", "scheduled"), accepted.text)
        [failed] = refused.json()["results"]
        self.assertEqual((failed["platform"], failed["action"]), ("*", "failed"), refused.text)
        assert_names_the_limit(self, failed["message"])
        self.assertEqual(self.ctx.store.get_post(ok["post_id"])["status"], "scheduled")  # the refusal changed nothing

    def test_mcp_accepts_the_horizon_lists_it_and_refuses_one_second_beyond(self):
        with frozen():
            error, accepted = self.tool("inkcast_publish", {"markdown": story_md(AT_HORIZON)})
            refused, message = self.tool("inkcast_publish", {"markdown": story_md(BEYOND)})
            sync_refused, sync_message = self.tool("inkcast_sync", {
                "source": self.id(), "files": {"posts/far.md": story_md(BEYOND)}})
        self.assertFalse(error, accepted)
        post_id = re.search(r"post `(pst_\w+)`", accepted).group(1)
        self.assertTrue(refused, message)
        assert_names_the_limit(self, message)
        self.assertFalse(sync_refused, sync_message)          # a sync reports per file, like a bad front matter
        self.assertIn("FAILED", sync_message)
        assert_names_the_limit(self, sync_message)
        listed_error, listed = self.tool("inkcast_posts", {"days_ahead": HORIZON_DAYS})
        self.assertFalse(listed_error, listed)
        self.assertIn(post_id, listed)
        too_far, _ = self.tool("inkcast_posts", {"days_ahead": HORIZON_DAYS + 1})
        self.assertTrue(too_far)
        cancel_error, cancelled = self.tool("inkcast_cancel", {"post_id": post_id})
        self.assertFalse(cancel_error, cancelled)
        self.assertIn("Cancelled", cancelled)

    def test_the_listing_caps_and_the_api_contract_use_the_constant(self):
        horizon = content.SCHEDULE_HORIZON_DAYS
        tools = {t.name: t for t in asyncio.run(self.ctx.extras["mcp"].list_tools())}
        self.assertEqual(tools["inkcast_posts"].input_schema["properties"]["days_ahead"]["maximum"], horizon)
        self.assertIn(f"{horizon} days", tools["inkcast_publish"].input_schema["properties"]["publish_at"]["description"])
        doc = openapi_document(self.config.public_url)
        [days_ahead] = [p for p in doc["paths"]["/api/v1/posts"]["get"]["parameters"] if p["name"] == "days_ahead"]
        self.assertEqual(days_ahead["schema"]["maximum"], horizon)
        self.assertIn(f"{horizon} days", doc["components"]["schemas"]["PublishRequest"]["properties"]["publish_at"]["description"])
        # The number is written once, as the constant: the caps can't drift from the validation again.
        package = pathlib.Path(content.__file__).resolve().parents[2]
        for rel in ("services/publishing/mcp_tools.py", "services/publishing/rest.py", "libs/contracts/openapi.py",
                    "libs/contracts/models.py"):
            self.assertNotRegex((package / rel).read_text(), rf"\b{horizon}\b",
                                f"{rel} spells the horizon out instead of using SCHEDULE_HORIZON_DAYS")


if __name__ == "__main__":
    unittest.main()
