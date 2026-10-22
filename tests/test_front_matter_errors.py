# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Front matter that is valid YAML but holds a value Python refuses (#107).

PyYAML's resolver accepts `2027-02-29T09:00:00+00:00` (no such day) and `2027-01-01T09:00:00+30:00` (an offset of
24 hours or more) as timestamps, then datetime raises a plain ValueError, which split_front_matter let through: it
caught yaml.YAMLError only. So REST publish and preview answered 500 instead of 422 invalid_story, MCP publish got an
unexplained "Error executing tool", and one such file failed a whole stories:sync of up to 50 files. Now the value
is a StoryError naming the field: every entry point refuses it the way it refuses any other bad front matter, and a
sync processes the other files and reports the bad one as `failed`."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.content import story as content
from inkcast.libs.http.app import compose
from test_e2e import free_port, serve

NO_SUCH_DAY = "2027-02-29T09:00:00+00:00"
OFFSET_TOO_BIG = "2027-01-01T09:00:00+30:00"
IMPOSSIBLE = (NO_SUCH_DAY, OFFSET_TOO_BIG)
KNOWN = ["medium", "devto"]


def story_md(publish_at: str, title: str = "A story") -> str:
    return f"---\ntitle: {title}\nplatforms: [devto]\npublish_at: {publish_at}\n---\n\nBody.\n"


def assert_names_the_field(test: unittest.TestCase, message: str, value: str, field: str = "publish_at") -> None:
    """The message says which field holds the value, and repeats the value as the file has it."""
    test.assertIn(field, message)
    test.assertIn(value, message)


class Parser(unittest.TestCase):
    """libs/content/story: the refusal is a StoryError, so every caller handles it like any other bad front matter."""

    def test_an_impossible_publish_at_is_a_story_error_naming_the_field(self):
        for value in IMPOSSIBLE:
            with self.subTest(value=value):
                with self.assertRaises(content.StoryError) as caught:
                    content.parse_story(story_md(value), known_platforms=KNOWN)
                assert_names_the_field(self, str(caught.exception), value)

    def test_any_field_is_named_and_the_value_is_refused_wherever_it_is(self):
        cases = [  # (front matter, what the message must say): the same escape through other fields and types
            ("title: 2027-02-29\n", ("title", "2027-02-29")),
            ("title: A story\ndevto:\n  publish_at: 2027-01-01T09:00:00+30:00\n", ("publish_at", "+30:00")),
            (f"title: A story\nsubtitle: {'1' * 4301}\n", ("subtitle", "4300 digits")),   # int() refuses the size
            ("title: A story\ndraft: !!bool maybe\n", ("draft", "maybe")),
            ("title: A story\npublish_at: !!timestamp soon\n", ("publish_at", "soon")),
            ("title: A story\ntags:\n  - 2027-02-29\n", ("line 4", "2027-02-29")),         # a list item: its file line
        ]
        for front_matter, expected in cases:
            with self.subTest(front_matter=front_matter[:48]):
                with self.assertRaises(content.StoryError) as caught:
                    content.parse_story(f"---\n{front_matter}---\n\nBody.\n", known_platforms=KNOWN)
                for part in expected:
                    self.assertIn(part, str(caught.exception))

    def test_possible_values_still_parse_as_before(self):
        now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        story = content.parse_story(
            "---\ntitle: 2048\ntags: [1, yes, 1.5]\npublish_at: 2027-02-28 09:00:00 +05:30\ndevto:\n  draft: true\n"
            "---\n\nBody.\n", known_platforms=KNOWN, now=now)
        self.assertEqual(story.title, "2048")
        self.assertEqual(story.tags, ["1", "True", "1.5"])
        self.assertEqual(story.publish_at, datetime(2027, 2, 28, 3, 30, tzinfo=timezone.utc))
        self.assertEqual(story.for_platform("devto")["draft"], True)
        with self.assertRaises(content.StoryError) as caught:  # YAML that isn't YAML is reported as before
            content.parse_story("---\ntitle: [unclosed\n---\n\nBody.\n", known_platforms=KNOWN)
        self.assertIn("isn't valid YAML", str(caught.exception))


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

    def test_rest_publish_and_preview_answer_422_invalid_story(self):
        for value in IMPOSSIBLE:
            for path in ("/api/v1/posts", "/api/v1/preview"):
                with self.subTest(value=value, path=path):
                    r = self.rest("POST", path, json={"markdown": story_md(value)})
                    self.assertEqual(r.status_code, 422, r.text)
                    self.assertEqual(r.json()["error"], "invalid_story")
                    assert_names_the_field(self, r.json()["message"], value)

    def test_rest_sync_fails_the_bad_file_and_processes_the_other_two(self):
        soon = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat()
        r = self.rest("POST", "/api/v1/stories:sync", json={"source": self.id(), "files": [
            {"path": "posts/first.md", "markdown": story_md(soon, "First")},
            {"path": "posts/bad.md", "markdown": story_md(NO_SUCH_DAY, "Bad")},
            {"path": "posts/third.md", "markdown": story_md(soon, "Third")},
        ]})
        self.assertEqual(r.status_code, 200, r.text)
        results = {x["path"]: x for x in r.json()["results"]}
        self.assertEqual(set(results), {"posts/first.md", "posts/bad.md", "posts/third.md"})
        bad = results["posts/bad.md"]
        self.assertEqual((bad["platform"], bad["action"]), ("*", "failed"), r.text)
        assert_names_the_field(self, bad["message"], NO_SUCH_DAY)
        for path in ("posts/first.md", "posts/third.md"):
            ok = results[path]
            self.assertEqual((ok["platform"], ok["action"], ok["status"]), ("devto", "create", "scheduled"), r.text)
            self.assertEqual(self.ctx.store.get_post(ok["post_id"])["status"], "scheduled")

    def test_mcp_publish_returns_a_tool_error_naming_the_field(self):
        for value in IMPOSSIBLE:
            with self.subTest(value=value):
                is_error, message = self.tool("inkcast_publish", {"markdown": story_md(value)})
                self.assertTrue(is_error, message)
                assert_names_the_field(self, message, value)


if __name__ == "__main__":
    unittest.main()
