# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #432 (#130): what still answered 500, or posted a wrong value, on 419f007.

1. ``models.Input``'s walk recursed once per level of the raw JSON. ``json.loads`` builds a body thousands of levels
   deep (2000 `[` are 4 KB), so a nested value anywhere in it, an ignored extra key included, raised RecursionError
   in the validator: 500 on publish, preview and the whole sync, where main answered 200, or 422 for a bad field."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

import httpx2
from pydantic import ValidationError

from fakes import Fakes
from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.contracts import models
from inkcast.libs.http.app import compose
from test_e2e import free_port, serve

DEEP = 3000  # levels: 6 KB of JSON, well inside the body limit, three times Python's default recursion limit
LONE = "\ud800"


def story_md(extra: str = "") -> str:
    soon = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat()
    return f"---\ntitle: A story\nplatforms: [devto]\npublish_at: {soon}\n{extra}---\n\nBody.\n"


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


class DeepInput(unittest.TestCase):
    """Finding 1: a deeply nested JSON value is the fields' to judge, as on main, never a RecursionError."""

    def test_the_models_judge_a_deep_value_as_their_fields_do(self):
        md = story_md()
        deep = json.loads(nested(DEEP))
        models.PublishRequest.model_validate({"markdown": md, "unknown": deep})  # an extra key is ignored, as on main
        models.SyncRequest.model_validate({"source": "s", "files": [{"path": "a.md", "markdown": md}], "unknown": deep})
        for model, data, where in (
            (models.PublishRequest, {"markdown": md, "platforms": deep}, "platforms.0"),
            (models.SyncRequest, {"source": "s", "files": [{"path": "a.md", "markdown": md}], "removed": deep}, "removed.0"),
            (models.TokenCreate, {"name": "ci", "scopes": deep}, "scopes.0"),
        ):
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValidationError) as caught:
                    model.model_validate(data)
                self.assertEqual(".".join(map(str, caught.exception.errors()[0]["loc"])), where)

    def test_a_lone_surrogate_where_the_models_keep_text_is_still_refused(self):
        for model, data, where in (
            (models.PublishRequest, {"markdown": story_md(), "destinations": {LONE: "x"}}, "destinations.[key]"),
            (models.SyncRequest, {"source": "s", "files": [{"path": "a.md", "markdown": "x" + LONE}]}, "files.0.markdown"),
        ):
            with self.subTest(where=where):
                with self.assertRaises(ValidationError) as caught:
                    model.model_validate(data)
                error = caught.exception.errors()[0]
                self.assertEqual((error["type"], ".".join(map(str, error["loc"]))), ("lone_surrogate", where))


class DeepBodyOverHttp(unittest.TestCase):
    """Finding 1 at the entry points: PublishingApi over real HTTP, DEV connected to the fake platforms."""

    @classmethod
    def setUpClass(cls):
        fport, port = free_port(), free_port()
        cls.fakes = Fakes(f"http://127.0.0.1:{fport}")
        cls.config = Config(public_url=f"http://127.0.0.1:{port}", port=port, secret_key="k" * 48,
                            database_path=":memory:", login="password", password="p" * 12,
                            platform_urls={"devto": f"{cls.fakes.base}/devto/api", "medium": f"{cls.fakes.base}/medium/v1"})
        specs = resolve("PublishingApi")
        ctx = build(cls.config, specs)
        cls.servers = [serve(cls.fakes.app(), fport), serve(compose(specs, ctx), port)]
        user = ctx.store.upsert_user("local:owner", "owner", None, None)["id"]
        ctx.store.save_credentials(user, "devto", {"api_key": "devto-key"}, {"id": "1", "name": "owner", "handle": "owner"})
        cls.token, _ = ctx.store.create_api_token(user, "test", ["posts:read", "posts:write", "stories:sync"], None)

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.should_exit = True

    def post(self, path: str, body: str) -> httpx2.Response:
        async def run():
            async with httpx2.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.token}",
                                                               "Content-Type": "application/json"}) as c:
                return await c.post(f"{self.config.public_url}{path}", content=body.encode())
        return asyncio.run(run())

    def test_a_deep_value_answers_as_on_main(self):
        md = json.dumps(story_md())
        cases = [  # (path, body, status, error code)
            ("/api/v1/preview", '{"markdown": %s, "unknown": %s}' % (md, nested(DEEP)), 200, None),
            ("/api/v1/posts", '{"markdown": %s, "unknown": %s}' % (md, nested(DEEP)), 200, None),
            ("/api/v1/preview", '{"markdown": %s, "platforms": %s}' % (md, nested(DEEP)), 422, "invalid_request"),
            ("/api/v1/stories:sync", '{"source": "deep", "files": [{"path": "a.md", "markdown": %s}], "unknown": %s}'
             % (md, nested(DEEP)), 200, None),
        ]
        for path, body, status, code in cases:
            with self.subTest(path=path, body=body[:40]):
                r = self.post(path, body)
                self.assertEqual(r.status_code, status, r.text[:200])
                if code:
                    self.assertEqual(r.json()["error"], code)


if __name__ == "__main__":
    unittest.main()
