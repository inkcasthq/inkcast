# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of the scheduling horizon (#95, PR #100)."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.content import story as content
from inkcast.libs.http.app import compose
from test_e2e import free_port, serve

# Beyond the horizon, written with a negative offset: in UTC it is in year 10000, which datetime can't hold.
OVERFLOWING = "9999-12-31T23:00:00-05:00"


def story_md(publish_at: str) -> str:
    return f"---\ntitle: Far ahead\nplatforms: [devto]\npublish_at: {publish_at}\n---\n\nBody.\n"


class Api(unittest.TestCase):
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


class BeyondTheHorizonOverflowingUtc(Api):
    """A publish_at beyond the horizon whose UTC form is past year 9999 must be refused like any other beyond-horizon
    time (422, per file in a sync), not crash the request with a 500."""

    def test_the_parser_refuses_it_naming_the_limit(self):
        with self.assertRaises(content.StoryError) as caught:
            content.parse_when(OVERFLOWING, datetime.now(timezone.utc))
        self.assertIn(f"{content.SCHEDULE_HORIZON_DAYS} days", str(caught.exception))

    def test_rest_publish_and_preview_answer_422(self):
        for path in ("/api/v1/posts", "/api/v1/preview"):
            r = self.rest("POST", path, json={"markdown": story_md(OVERFLOWING)})
            self.assertEqual(r.status_code, 422, f"{path}: {r.text}")
            self.assertEqual(r.json()["error"], "invalid_story")

    def test_sync_fails_that_file_only(self):
        ok_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
        r = self.rest("POST", "/api/v1/stories:sync", json={"source": self.id(), "files": [
            {"path": "posts/far.md", "markdown": story_md(OVERFLOWING)},
            {"path": "posts/ok.md", "markdown": story_md(ok_at)}]})
        self.assertEqual(r.status_code, 200, r.text)
        by_path = {x["path"]: x for x in r.json()["results"]}
        self.assertEqual((by_path["posts/far.md"]["platform"], by_path["posts/far.md"]["action"]), ("*", "failed"))
        self.assertEqual((by_path["posts/ok.md"]["action"], by_path["posts/ok.md"]["status"]), ("create", "scheduled"))


if __name__ == "__main__":
    unittest.main()
