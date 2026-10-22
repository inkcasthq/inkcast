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

from inkcast.cli import render_table
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


class ScalarTagOnAMapping(Api):
    """PyYAML's scalar constructors also take a mapping that holds YAML 1.1's value key (`!!bool {=: yes}` is True),
    so the node the PR guards isn't always a ScalarNode. `!!timestamp {=: soon}` is a TypeError (PyYAML matches its
    regexp against the node's list of pairs), which the PR doesn't catch; a refused `!!bool {=: maybe, ...}` hands
    _RefusedValue a MappingNode, whose `value[:79] + "…"` is a TypeError past 80 pairs. Each is a 500 on REST publish
    and for the whole sync. Under 80 pairs the message is the repr of every node: unbounded, and not the value."""

    def test_a_scalar_tag_on_a_mapping_is_refused(self):
        pairs = ", ".join(f"k{i}: {i}" for i in range(81))
        for extra in ("devto:\n  publish_at: !!timestamp {=: soon}\n", f"draft: !!bool {{=: maybe, {pairs}}}\n"):
            with self.subTest(extra=extra[:40]):
                self.assert_refused(extra)

    def test_the_message_does_not_repeat_the_mapping(self):
        message = self.assert_refused("draft: !!bool {=: maybe, k: " + "x" * 5000 + "}\n")
        self.assertFalse("ScalarNode" in message, message[:300])
        self.assertLess(len(message), 500)


class KeyEchoedRaw(Api):
    """The PR shows the refused value with repr and caps it at 80 characters, but names the key it sits under as it
    is, and a key is any YAML string. `"\\ud800"` (an escape for a lone surrogate) can't be encoded in the JSON
    response: REST publish answers 500, and so does the whole sync. `"x\\n::warning::..."` puts a line break in the
    message, which the CLI prints at the start of a line (a workflow command in a GitHub Actions log, from a file in
    the repository). An explicit key (`? ...`) has no length limit, so the message has none either."""

    CASES = {
        "lone surrogate": '"\\ud800": 2027-02-29\n',
        "line break": '"x\\n::warning::injected": 2027-02-29\n',
        "escape sequence": '"\\e[2J": 2027-02-29\n',
        "100 KB": '? "' + "k" * 100_000 + '"\n: 2027-02-29\n',
    }

    def test_a_key_that_is_not_short_plain_text_is_not_put_in_the_message(self):
        for name, extra in self.CASES.items():
            with self.subTest(name):
                message = self.assert_refused(extra)
                self.assertTrue(message.isprintable(), message[:300])
                self.assertLess(len(message), 500)
                self.assertIn("'2027-02-29' isn't a valid timestamp", message)
                table = render_table([{"action": "failed", "platform": "*", "path": "posts/bad.md", "message": message}])
                self.assertFalse(any(line.startswith("::") for line in table.splitlines()), table[:300])


class ReasonUncapped(Api):
    """The value is cut to 80 characters, but Python's reason is added whole, and float()'s quotes the whole value
    (int()'s stops at 200): `draft: !!float <100 KB>` is a 100 KB message, in REST's answer and in the sync's."""

    def test_the_message_stays_short_for_a_long_float(self):
        message = self.assert_refused("draft: !!float " + "x" * 100_000 + "\n")
        self.assertIn("draft", message)
        self.assertLess(len(message), 500)


if __name__ == "__main__":
    unittest.main()
