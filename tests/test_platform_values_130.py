# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A bad value in a platform block, or a lone surrogate in a text field, is the file's or the request's error, never
a 500 (#130; the concerns left open by the review of #123, PR #129).

On main: `devto: {tags: 5}` raised StoryError at prepare time, where only PlatformError is handled, so REST publish,
preview and a whole `stories:sync` answered 500; `devto: {series: 2027-01-01}` (a date; `!!binary`, `!!set` and a
timestamp alike) previewed, then publish and the sync answered 500 encoding the post row or the payload hash;
`medium: {license: [x]}` and `medium: {publication: [a]}` crashed the plug-in (500); a `"\\ud800"` escape in a
request field or in the front matter passed validation, then the response's UTF-8 encoding failed (500), or the
request to the platform did, which recorded the post as unconfirmed. MCP answered the bare "Error executing tool".

Now the values are made JSON-safe, or refused, when the story is parsed (``story._platform_block``); a plug-in that
crashes in ``prepare`` is that platform's failure (``Publisher.prepare``); every request model refuses an unpaired
surrogate in any text field, naming it (``models.Input``), and so does the front matter. The entry points are driven
as in test_front_matter_errors: PublishingApi over real HTTP, DEV and Medium connected to the suite's fake platforms,
a personal access token for every scope."""

import asyncio
import itertools
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from fakes import Fakes
from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.content import story as content
from inkcast.libs.contracts import models
from inkcast.libs.contracts.openapi import REQUEST_MODELS
from inkcast.libs.http.app import compose
from inkcast.libs.platforms import Account, Platform, Prepared, PublishResult
from test_e2e import free_port, serve

KNOWN = ["medium", "devto"]
LONE = "\ud800"  # an unpaired surrogate: what the JSON escape "\ud800" decodes to
SURROGATE_MESSAGE = "has an unpaired surrogate, U+D800 at character"


def story_md(extra: str = "", title: str = "A story", platforms: str = "[devto]", scheduled: bool = True,
             body: str = "Body.\n") -> str:
    """A story a week ahead (nothing is sent) unless ``scheduled`` is false; ``extra`` is more front matter, whole lines."""
    soon = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat()
    when = f"publish_at: {soon}\n" if scheduled else ""
    return f"---\ntitle: {title}\nplatforms: {platforms}\n{when}{extra}---\n\n{body}"


# (front matter lines, what the message must say): refused when the story is parsed, so every entry point refuses them.
REFUSED = [
    ("devto:\n  tags: 5\n", "devto.tags must be a list or a comma-separated string"),
    ("devto:\n  tags: {a: b}\n", "devto.tags must be a list or a comma-separated string"),
    ("devto:\n  tags: [[a, b]]\n", "devto.tags must be a list of names or a comma-separated string; ['a', 'b'] isn't a name"),
    ("tags: [[a, b]]\n", "tags must be a list of names or a comma-separated string; ['a', 'b'] isn't a name"),
    ("devto:\n  series: !!binary aGk=\n", "devto.series is binary data (!!binary), which JSON can't carry"),
    ("devto:\n  series: !!set {a: null}\n", "devto.series is a set (!!set), which JSON can't carry"),
    ("devto:\n  series: .nan\n", "devto.series is nan, which JSON can't carry"),
    ("devto:\n  1: x\n", "devto has a key that isn't text (1); quote it, or remove it"),
    ("devto:\n  extra: {2027-01-01: x}\n", "devto.extra has a key that isn't text (datetime.date(2027, 1, 1))"),
    ("devto:\n  extra: [1, !!binary aGk=]\n", "devto.extra[1] is binary data (!!binary)"),
    ('subtitle: "\\ud800"\n', f"subtitle {SURROGATE_MESSAGE} 1"),
    ('tags: [a, "b\\udc00"]\n', "tags[1] has an unpaired surrogate, U+DC00 at character 2"),
    ('devto:\n  series: "x\\ud800"\n', f"devto.series {SURROGATE_MESSAGE} 2"),
    ('"\\ud800": 1\n', f"A key in the front matter {SURROGATE_MESSAGE} 1"),
    ('devto:\n  "\\ud800": 1\n', f"A key in devto {SURROGATE_MESSAGE} 1"),
    ("devto:\n  series: .inf\n", "devto.series is inf, which JSON can't carry"),
    ("devto:\n  series: -.inf\n", "devto.series is -inf, which JSON can't carry"),
]


class Parser(unittest.TestCase):
    """libs/content/story: refused at parse time, as a StoryError naming where, or normalised to what JSON carries."""

    def test_each_bad_value_is_a_story_error_naming_where_it_is(self):
        for extra, expected in REFUSED:
            with self.subTest(extra=extra[:40]):
                with self.assertRaises(content.StoryError) as caught:
                    content.parse_story(story_md(extra), known_platforms=KNOWN)
                self.assertIn(expected, str(caught.exception))

    def test_platform_block_values_are_what_json_carries(self):
        story = content.parse_story(story_md(
            "devto:\n  series: 2027-01-01\n  when: 2027-01-01T09:00:00+00:00\n  pair: [1, 2.5]\n  nested: {a: [b]}\n"
            "  draft: true\n  none: ~\n  tags: [AI, ai, '#python', 2027-01-01]\n"), known_platforms=KNOWN)
        fields = story.for_platform("devto")
        self.assertEqual(fields["series"], "2027-01-01")                   # a plain date, as the person wrote it
        self.assertEqual(fields["when"], "2027-01-01T09:00:00+00:00")      # a timestamp, as ISO 8601 text
        self.assertEqual((fields["pair"], fields["nested"], fields["draft"], fields["none"]), ([1, 2.5], {"a": ["b"]}, True, None))
        self.assertEqual(fields["tags"], ["AI", "python", "2027-01-01"])  # made once, at parse time; a date is still a name
        json.dumps(story.overrides)                                        # what the post row and the payload hash do

    def test_the_message_never_echoes_a_key_that_is_not_short_plain_text(self):
        cases = {  # the rule the review of #123 set for refused values (#129): a line break, a lone surrogate, 100 KB
            "line break": 'devto:\n  "x\\n::warning::injected": !!binary aGk=\n',
            "long key": "devto:\n  ? " + "k" * 100_000 + "\n  : !!binary aGk=\n",
            "surrogate value under a line-break key": 'devto:\n  "x\\n::w": "\\ud800"\n',
        }
        for name, extra in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(content.StoryError) as caught:
                    content.parse_story(story_md(extra), known_platforms=KNOWN)
                message = str(caught.exception)
                self.assertTrue(message.startswith("The value under a key in devto"), message[:120])
                self.assertNotIn("\n", message)
                self.assertLess(len(message), 300)

    def test_a_bad_platforms_value_names_platforms(self):
        with self.assertRaises(content.StoryError) as caught:
            content.parse_story("---\ntitle: A story\nplatforms: {a: b}\n---\n\nBody.\n", known_platforms=KNOWN)
        self.assertIn("platforms must be a list or a comma-separated string", str(caught.exception))


class Models(unittest.TestCase):
    """libs/contracts/models: every request model refuses an unpaired surrogate in every text field, at its path."""

    def test_every_request_model_checks_its_text(self):
        for model in REQUEST_MODELS:
            self.assertTrue(issubclass(model, models.Input), model.__name__)

    def test_each_text_field_is_refused_at_its_path(self):
        md = story_md()
        cases: list[tuple[type[models.Input], dict[str, Any], str]] = [
            (models.PublishRequest, {"markdown": md + LONE}, "markdown"),
            (models.PublishRequest, {"markdown": md, "title": "T " + LONE}, "title"),
            (models.PublishRequest, {"markdown": md, "subtitle": LONE}, "subtitle"),
            (models.PublishRequest, {"markdown": md, "tags": ["a", LONE]}, "tags.1"),
            (models.PublishRequest, {"markdown": md, "platforms": [LONE]}, "platforms.0"),
            (models.PublishRequest, {"markdown": md, "canonical_url": LONE}, "canonical_url"),
            (models.PublishRequest, {"markdown": md, "cover_image": LONE}, "cover_image"),
            (models.PublishRequest, {"markdown": md, "destinations": {"medium": LONE}}, "destinations.medium"),
            (models.PublishRequest, {"markdown": md, "destinations": {LONE: "x"}}, "destinations.[key]"),
            (models.PublishRequest, {"markdown": md, "publish_at": LONE}, "publish_at"),
            (models.PublishRequest, {"markdown": md, "image_base_url": LONE}, "image_base_url"),
            (models.SyncRequest, {"source": LONE, "files": [{"path": "p.md", "markdown": md}]}, "source"),
            (models.SyncRequest, {"source": "s", "ref": LONE, "files": [{"path": "p.md", "markdown": md}]}, "ref"),
            (models.SyncRequest, {"source": "s", "platforms": [LONE], "files": [{"path": "p.md", "markdown": md}]}, "platforms.0"),
            (models.SyncRequest, {"source": "s", "image_base_url": LONE, "files": [{"path": "p.md", "markdown": md}]}, "image_base_url"),
            (models.SyncRequest, {"source": "s", "files": [{"path": "p" + LONE, "markdown": md}]}, "files.0.path"),
            (models.SyncRequest, {"source": "s", "files": [{"path": "p.md", "markdown": md + LONE}]}, "files.0.markdown"),
            (models.SyncRequest, {"source": "s", "files": [{"path": "p.md", "markdown": md, "image_base_url": LONE}]}, "files.0.image_base_url"),
            (models.SyncRequest, {"source": "s", "removed": ["a.md", "b" + LONE]}, "removed.1"),
            (models.TokenCreate, {"name": "ci " + LONE, "scopes": ["stories:sync"]}, "name"),
            (models.TokenCreate, {"name": "ci", "scopes": ["stories:sync", LONE]}, "scopes.1"),
        ]
        for model, data, where in cases:
            with self.subTest(model=model.__name__, where=where):
                with self.assertRaises(ValidationError) as caught:
                    model.model_validate(data)
                errors = caught.exception.errors()
                self.assertEqual((errors[0]["type"], ".".join(map(str, errors[0]["loc"]))), ("lone_surrogate", where))
                self.assertIn(SURROGATE_MESSAGE, errors[0]["msg"])

    def test_a_sync_request_built_from_sync_files_is_checked_too(self):
        """The MCP tool builds SyncFile objects first (inkcast_sync): their text is walked by name, like the JSON's."""
        with self.assertRaises(ValidationError) as caught:
            models.SyncRequest(source="s", files=[models.SyncFile(path="p.md", markdown=story_md() + LONE)])
        self.assertEqual(".".join(map(str, caught.exception.errors()[0]["loc"])), "files.0.markdown")
        ok = models.SyncRequest(source="s", files=[models.SyncFile(path="p.md", markdown=story_md("subtitle: Fine\n"))])
        self.assertEqual(len(ok.files), 1)


class Crashy(Platform):
    """A plug-in whose prepare crashes on a value JSON carries fine: the pipeline's problem, not a 500 (#130)."""

    id = "crashy"
    name = "Crashy"
    default_api_url = "http://127.0.0.1:9/crashy"
    credential_fields = []

    async def verify(self, creds):
        return Account(id="c", name="Crashy")

    def prepare(self, fields, body) -> Prepared:
        return Prepared(platform=self.id, title=fields["title"], payload={"n": int(fields.get("n", 0))}, preview={})

    async def publish(self, creds, account, prepared, destination) -> PublishResult:
        raise AssertionError("never sent: prepare crashed")


class EntryPoints(unittest.TestCase):
    """PublishingApi over real HTTP: DEV and Medium connected to the fake platforms, a token for every scope."""

    sources = itertools.count()

    @classmethod
    def setUpClass(cls):
        fport, port = free_port(), free_port()
        cls.fakes = Fakes(f"http://127.0.0.1:{fport}")
        cls.config = Config(public_url=f"http://127.0.0.1:{port}", port=port, secret_key="k" * 48,
                            database_path=":memory:", login="password", password="p" * 12,
                            platform_urls={"medium": f"{cls.fakes.base}/medium/v1", "devto": f"{cls.fakes.base}/devto/api"})
        specs = resolve("PublishingApi")
        cls.ctx = build(cls.config, specs)
        cls.ctx.platforms["crashy"] = Crashy(http=None)  # the registry the service, the publisher and the sync share
        cls.servers = [serve(cls.fakes.app(), fport), serve(compose(specs, cls.ctx), port)]
        store = cls.ctx.store
        cls.user = store.upsert_user("local:owner", "owner", None, None)["id"]
        store.save_credentials(cls.user, "devto", {"api_key": "devto-key"}, {"id": "1", "name": "owner", "handle": "owner"})
        store.save_credentials(cls.user, "medium", {"token": "medium-token"}, {"id": "m-ada", "name": "Ada", "handle": "ada"})
        store.save_credentials(cls.user, "crashy", {"k": "v"}, {"id": "c", "name": "Crashy"})
        cls.token, _ = store.create_api_token(cls.user, "test", ["posts:read", "posts:write", "stories:sync"], None)

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.should_exit = True

    def rest(self, path: str, body: dict) -> httpx2.Response:
        """JSON as a client sends it, ASCII-escaped: a lone surrogate travels as the escape `\\ud800` (httpx2's own
        json= encodes UTF-8 and would refuse it, as every encoder does; the server's parser accepts it)."""
        async def run():
            async with httpx2.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.token}",
                                                               "Content-Type": "application/json"}) as c:
                return await c.post(f"{self.config.public_url}{path}", content=json.dumps(body).encode("ascii"))
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

    def sync_three(self, bad: str, **extra) -> tuple[httpx2.Response, dict[str, dict]]:
        """A sync of a good file, ``bad`` and another good file: the response and its results by path."""
        r = self.rest("/api/v1/stories:sync", {"source": f"{self.id()}:{next(self.sources)}", "files": [
            {"path": "posts/first.md", "markdown": story_md(title="First")},
            {"path": "posts/bad.md", "markdown": bad},
            {"path": "posts/third.md", "markdown": story_md(title="Third")},
        ], **extra})
        return r, {x["path"]: x for x in r.json().get("results", [])}

    def assert_others_scheduled(self, results: dict[str, dict], text: str) -> None:
        for path in ("posts/first.md", "posts/third.md"):
            ok = results[path]
            self.assertEqual((ok["platform"], ok["action"], ok["status"]), ("devto", "create", "scheduled"), text)
            self.assertEqual(self.ctx.store.get_post(ok["post_id"])["status"], "scheduled")

    def test_a_bad_front_matter_value_is_422_on_publish_and_preview_and_the_file_fails_in_a_sync(self):
        for extra, expected in REFUSED:
            with self.subTest(extra=extra[:40]):
                markdown = story_md(extra)
                for path in ("/api/v1/posts", "/api/v1/preview"):
                    r = self.rest(path, {"markdown": markdown})
                    self.assertEqual(r.status_code, 422, r.text[:300])
                    self.assertEqual(r.json()["error"], "invalid_story")
                    self.assertIn(expected, r.json()["message"])
                r, results = self.sync_three(markdown)
                self.assertEqual(r.status_code, 200, r.text[:300])
                bad = results["posts/bad.md"]
                self.assertEqual((bad["platform"], bad["action"]), ("*", "failed"), r.text[:300])
                self.assertIn(expected, bad["message"])
                self.assertEqual(len(results), 3, r.text[:300])
                self.assert_others_scheduled(results, r.text[:300])

    def test_a_date_in_a_platform_block_is_its_iso_text_everywhere(self):
        for value, text in (("2027-01-01", "2027-01-01"), ("2027-01-01T09:00:00+00:00", "2027-01-01T09:00:00+00:00")):
            with self.subTest(value=value):
                markdown = story_md(f"devto:\n  series: {value}\n")
                r = self.rest("/api/v1/preview", {"markdown": markdown})
                self.assertEqual(r.status_code, 200, r.text[:300])
                self.assertEqual(r.json()["results"][0]["preview"]["series"], text)
                r = self.rest("/api/v1/posts", {"markdown": markdown})
                self.assertEqual(r.status_code, 200, r.text[:300])
                post = self.ctx.store.get_post(r.json()["results"][0]["post_id"])
                self.assertEqual(json.loads(post["payload"])["payload"]["article"]["series"], text)  # the stored row
                r, results = self.sync_three(markdown)
                self.assertEqual(r.status_code, 200, r.text[:300])
                self.assertEqual((results["posts/bad.md"]["action"], results["posts/bad.md"]["status"]), ("create", "scheduled"))

    def test_a_value_a_plugin_cannot_take_is_that_platforms_failure_and_the_rest_is_answered(self):
        """JSON-safe, but not what the plug-in expects: Medium's license and publication as lists, and a plug-in that
        crashes on it. Each is one platform's `failed` (the request 200, with a message) and the sync's other files
        still run; a crash is logged with the platform's id, and its text never reaches the reply."""
        cases = {  # extra front matter → (platform, what its failure must say)
            "medium:\n  license: [x]\n": ("medium", "medium.license must be one of"),
            "medium:\n  publication: [a]\n": ("medium", "No publication matching \"['a']\" on Medium"),
            "crashy:\n  n: x\n": ("crashy", "Internal error in the Crashy plug-in while preparing this story; check its `crashy:` block"),
        }
        for extra, (pid, expected) in cases.items():
            with self.subTest(extra=extra[:30]):
                markdown = story_md(extra, platforms=f"[{pid}]", scheduled=False)
                for path in ("/api/v1/posts", "/api/v1/preview"):
                    if pid == "medium" and "publication" in extra and path.endswith("preview"):
                        continue  # a preview resolves no destination: the lookup's refusal is the publish's
                    with self.assertLogs("inkcast.libs.pipeline", level="ERROR") if pid == "crashy" else self.assertNoLogs(level="ERROR") as logs:
                        r = self.rest(path, {"markdown": markdown})
                    self.assertEqual(r.status_code, 200, r.text[:300])
                    result = r.json()["results"][0]
                    self.assertEqual((result["platform"], result["status"]), (pid, "failed"), r.text[:300])
                    self.assertIn(expected, result["message"])
                    self.assertNotIn("invalid literal", result["message"])  # the crash's own text stays in the log
                    if pid == "crashy":
                        self.assertIn("Platform crashy crashed preparing a story", logs.output[0])
                        self.assertIn("invalid literal for int()", logs.output[0])
                r, results = self.sync_three(markdown)
                self.assertEqual(r.status_code, 200, r.text[:300])
                bad = results["posts/bad.md"]
                self.assertEqual((bad["platform"], bad["action"]), (pid, "failed"), r.text[:300])
                self.assertIn(expected, bad["message"])
                self.assert_others_scheduled(results, r.text[:300])

    def test_a_lone_surrogate_in_a_request_field_is_422_naming_the_field(self):
        plain = story_md(scheduled=False)
        cases = [  # (path, body, the field named)
            ("/api/v1/posts", {"markdown": plain, "title": "T " + LONE}, "title"),
            ("/api/v1/preview", {"markdown": plain, "title": "T " + LONE}, "title"),
            ("/api/v1/posts", {"markdown": plain, "platforms": [LONE]}, "platforms.0"),
            ("/api/v1/posts", {"markdown": plain, "tags": [LONE]}, "tags.0"),
            ("/api/v1/posts", {"markdown": story_md(body=f"Body {LONE}\n", scheduled=False)}, "markdown"),
            ("/api/v1/preview", {"markdown": story_md(body=f"Body {LONE}\n", scheduled=False)}, "markdown"),
            ("/api/v1/stories:sync", {"source": "s " + LONE, "files": [{"path": "p.md", "markdown": plain}]}, "source"),
            ("/api/v1/stories:sync", {"source": "s", "files": [{"path": "p" + LONE, "markdown": plain}]}, "files.0.path"),
            ("/api/v1/stories:sync", {"source": "s", "files": [{"path": "p.md", "markdown": plain + LONE}]}, "files.0.markdown"),
        ]
        for path, body, field in cases:
            with self.subTest(path=path, field=field):
                r = self.rest(path, body)
                self.assertEqual(r.status_code, 422, r.text[:300])
                self.assertEqual(r.json()["error"], "invalid_request")
                self.assertIn(f"{field}: {SURROGATE_MESSAGE}", r.json()["message"])
        # Nothing was published or recorded on the way to a refusal: the body with the surrogate never reached DEV.
        self.assertFalse(any("Body \ud800" in a.get("body_markdown", "") for a in self.fakes.devto_articles))

    def test_mcp_gets_a_tool_error_naming_the_field_and_its_sync_runs_the_other_files(self):
        for extra, expected in [REFUSED[0], REFUSED[4], REFUSED[10]]:  # tags: 5, !!binary, the subtitle escape
            with self.subTest(extra=extra[:30]):
                is_error, text = self.tool("inkcast_publish", {"markdown": story_md(extra)})
                self.assertTrue(is_error, text)
                self.assertIn(expected, text)
                is_error, text = self.tool("inkcast_sync", {"source": f"{self.id()}:{next(self.sources)}", "files": {
                    "posts/first.md": story_md(title="First"), "posts/bad.md": story_md(extra), "posts/third.md": story_md(title="Third")}})
                self.assertFalse(is_error, text)
                self.assertIn(f"posts/bad.md → *: FAILED\n  {expected}", text)
                for path in ("posts/first.md", "posts/third.md"):
                    self.assertIn(f"{path} → devto: CREATE (scheduled)", text)

    def test_the_log_never_carries_the_story(self):
        """Not the title, the subtitle or the body, on any logger of the server, at any level, through REST publish,
        preview and sync and MCP publish of a story that is accepted. The MCP client in this process logs what it
        sends at DEBUG (`mcp.client.*`): that is the caller's log, not the server's, and is left out."""
        title, subtitle, body = "Title-5f2c9e", "Subtitle-8a1d4b", "Body-c3e7f0 paragraph.\n"
        markdown = story_md(f"subtitle: {subtitle}\n", title=title, body=body)
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record) if not record.name.startswith("mcp.client") else None
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            self.assertEqual(self.rest("/api/v1/posts", {"markdown": markdown}).status_code, 200)
            self.assertEqual(self.rest("/api/v1/preview", {"markdown": markdown}).status_code, 200)
            r, _ = self.sync_three(markdown)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(self.tool("inkcast_publish", {"markdown": markdown, "dry_run": True})[0])
        finally:
            root.setLevel(level)
            root.removeHandler(handler)
        self.assertGreater(len(records), 0)
        for record in records:
            line = record.getMessage() + " " + str(vars(record))
            for marker in (title, subtitle, body.strip()):
                self.assertNotIn(marker, line, line[:200])


if __name__ == "__main__":
    unittest.main()
