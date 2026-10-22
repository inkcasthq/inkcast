# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #123 (#107): front matter that still escapes as a 500, or whose refusal breaks the rules
the PR set for its own message (the value shown with repr, at most 80 characters).

One class per finding. The entry points are driven as in test_front_matter_errors: PublishingApi over real HTTP, DEV
connected, a personal access token for every scope. A refused file must be a 422 `invalid_story` on REST publish, and
in a sync beside a good file it must be `failed` while the good one is still scheduled."""

import asyncio
import itertools
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http.app import compose
from test_e2e import free_port, serve


def story_md(extra: str, title: str = "A story") -> str:
    """A story for DEV a week ahead, so nothing is sent; `extra` is more front matter, one or more whole lines."""
    soon = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat()
    return f"---\ntitle: {title}\nplatforms: [devto]\npublish_at: {soon}\n{extra}---\n\nBody.\n"


class Api(unittest.TestCase):
    """PublishingApi over real HTTP, as test_front_matter_errors.EntryPoints."""

    sources = itertools.count()

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
            async with httpx2.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {self.token}"}) as c:
                return await c.request(method, f"{self.config.public_url}{path}", **kw)
        return asyncio.run(run())

    def assert_refused(self, extra: str) -> str:
        """REST publish answers 422 `invalid_story`; a sync of a good file and this one answers 200, with this one
        `failed` and the good one scheduled. Returns the message, which both give."""
        markdown = story_md(extra)
        r = self.rest("POST", "/api/v1/posts", json={"markdown": markdown})
        self.assertEqual(r.status_code, 422, r.text[:300])
        self.assertEqual(r.json()["error"], "invalid_story")
        s = self.rest("POST", "/api/v1/stories:sync", json={"source": f"{self.id()}:{next(self.sources)}", "files": [
            {"path": "posts/good.md", "markdown": story_md("", "Good")},
            {"path": "posts/bad.md", "markdown": markdown},
        ]})
        self.assertEqual(s.status_code, 200, s.text[:300])
        results = {x["path"]: x for x in s.json()["results"]}
        bad, good = results["posts/bad.md"], results["posts/good.md"]
        self.assertEqual((bad["platform"], bad["action"], bad["message"]), ("*", "failed", r.json()["message"]))
        self.assertEqual((good["platform"], good["action"], good["status"]), ("devto", "create", "scheduled"))
        return r.json()["message"]


class EmptyNumber(Api):
    """`!!int ''` and `!!float ''`: PyYAML reads the scalar's first character to find its sign, so an empty one is an
    IndexError, which the PR's (ValueError, KeyError, AttributeError) lets through: REST publish answers 500, and so
    does the whole sync, which is #107 again for an explicit tag the PR otherwise covers (`!!int x`, `!!float x`)."""

    def test_an_empty_int_or_float_is_refused_naming_the_field(self):
        for tag in ("int", "float"):
            with self.subTest(tag=tag):
                self.assertIn("draft", self.assert_refused(f"draft: !!{tag} ''\n"))


if __name__ == "__main__":
    unittest.main()
