"""Git publishing end to end: personal access tokens, scopes, the publish ledger, in-place updates,
no duplicates (ever), the `inkcast sync` CLI, and the browser flow that shows a new token once.
Runs against the single-process server and again against split services behind the Gateway."""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import httpx2

from inkcast.cli import sync_main
from test_e2e import Harness
from test_split_mode import SplitHarness


def story(title: str, body: str = "Body.", extra: str = "", platforms: str = "[devto, medium]") -> str:
    return (f"---\ntitle: {title}\ntags: [python]\ncanonical_url: https://blog.example.com/{title.lower().replace(' ', '-')}\n"
            f"platforms: {platforms}\n{extra}---\n\n{body}\n")


class GitPublishing(Harness):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = cls.config.public_url
        harness = cls("test_tokens_and_scopes")
        cls.oauth = harness.access_token()
        h = {"Authorization": f"Bearer {cls.oauth}"}

        async def setup():
            async with httpx2.AsyncClient(timeout=30) as c:
                for pid, fields in (("devto", {"api_key": "devto-key"}), ("medium", {"token": "medium-token"})):
                    link = (await c.post(f"{cls.base}/api/v1/platforms/{pid}/connect-link", headers=h)).json()
                    await c.post(link["url"], data=fields)
                r = await c.post(f"{cls.base}/api/v1/tokens", headers=h,
                                 json={"name": "blog CI", "scopes": ["stories:sync", "posts:read"]})
                return r.json()
        cls.pat = asyncio.run(setup())

    def call(self, method: str, path: str, token: str | None = None, **kw) -> httpx2.Response:
        headers = {"Authorization": f"Bearer {token or self.pat['token']}", **kw.pop("headers", {})}
        async def run():
            async with httpx2.AsyncClient(timeout=30) as c:
                return await c.request(method, f"{self.base}{path}", headers=headers, **kw)
        return asyncio.run(run())

    def sync(self, files: dict[str, str], source: str | None = None, **extra) -> dict:
        body = {"source": source or self.id(), "files": [{"path": p, "markdown": m} for p, m in files.items()], **extra}
        r = self.call("POST", "/api/v1/stories:sync", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return {(x["path"], x["platform"]): x for x in r.json()["results"]}

    # ---------------------------------------------------------------------------------------------

    def test_tokens_and_scopes(self):
        self.assertTrue(self.pat["token"].startswith("ink_pat_"))
        self.assertEqual(self.pat["scopes"], ["posts:read", "stories:sync"])
        listed = self.call("GET", "/api/v1/tokens", token=self.oauth).json()
        self.assertIn(self.pat["id"], {t["id"] for t in listed})
        self.assertNotIn("token", listed[0])                                   # never listed again
        self.assertEqual(self.call("GET", "/api/v1/tokens").status_code, 403)  # a PAT can't manage tokens
        self.assertEqual(self.call("POST", "/api/v1/tokens", json={"name": "x", "scopes": ["posts:write"]}).status_code, 403)
        bad_scope = self.call("POST", "/api/v1/tokens", token=self.oauth, json={"name": "x", "scopes": ["root"]})
        self.assertEqual(bad_scope.status_code, 422)
        self.assertEqual(bad_scope.headers["content-type"], "application/problem+json")
        # posts:read allows a preview but not a publish; connections:read is not granted.
        preview = self.call("POST", "/api/v1/preview", json={"markdown": story("Scope check")})
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.json()["dry_run"])
        publish = self.call("POST", "/api/v1/posts", json={"markdown": story("Scope check")})
        self.assertEqual(publish.status_code, 403)
        self.assertIn("posts:write", publish.json()["message"])
        self.assertEqual(self.call("GET", "/api/v1/platforms").status_code, 403)
        self.assertEqual(self.call("GET", "/api/v1/me").status_code, 200)

    def test_revoked_and_expired_tokens_stop_working(self):
        made = self.call("POST", "/api/v1/tokens", token=self.oauth, json={"name": "short", "scopes": ["posts:read"]}).json()
        self.assertEqual(self.call("GET", "/api/v1/ledger", token=made["token"]).status_code, 200)
        self.assertEqual(self.call("DELETE", f"/api/v1/tokens/{made['id']}", token=self.oauth).status_code, 200)
        self.assertEqual(self.call("GET", "/api/v1/ledger", token=made["token"]).status_code, 401)
        self.assertEqual(self.call("DELETE", f"/api/v1/tokens/{made['id']}", token=self.oauth).status_code, 404)
        tampered = self.pat["token"][:-4] + "AAAA"
        self.assertEqual(self.call("GET", "/api/v1/ledger", token=tampered).status_code, 401)

    def test_sync_creates_then_skips_then_updates_never_duplicates(self):
        devto0, medium0 = len(self.fakes.devto_articles), len(self.fakes.medium_posts)
        files = {"posts/a.md": story("Alpha"), "posts/b.md": story("Beta")}
        first = self.sync(files)
        self.assertEqual({r["action"] for r in first.values()}, {"create"})
        self.assertEqual(len(self.fakes.devto_articles), devto0 + 2)
        self.assertEqual(len(self.fakes.medium_posts), medium0 + 2)
        self.assertTrue(first[("posts/a.md", "devto")]["url"].startswith("https://dev.to/"))

        again = self.sync(files)
        self.assertEqual({r["action"] for r in again.values()}, {"unchanged"})

        edited = self.sync({**files, "posts/a.md": story("Alpha", body="A better body.")})
        self.assertEqual(edited[("posts/a.md", "devto")]["action"], "update")
        self.assertEqual(edited[("posts/a.md", "medium")]["action"], "stale")      # Medium can't edit
        self.assertIn("doesn't let Inkcast edit", edited[("posts/a.md", "medium")]["message"])
        self.assertEqual(edited[("posts/b.md", "devto")]["action"], "unchanged")
        self.assertEqual(self.fakes.devto_updates[-1][1]["body_markdown"], "A better body.\n")
        # Nothing was ever created twice.
        self.assertEqual(len(self.fakes.devto_articles), devto0 + 2)
        self.assertEqual(len(self.fakes.medium_posts), medium0 + 2)
        # Still stale on the next run (until the file is reverted), never re-posted.
        self.assertEqual(self.sync({**files, "posts/a.md": story("Alpha", body="A better body.")})
                         [("posts/a.md", "medium")]["action"], "stale")
        self.assertEqual(len(self.fakes.medium_posts), medium0 + 2)

        ledger = self.call("GET", f"/api/v1/ledger?source={self.id()}").json()
        self.assertEqual({(e["path"], e["platform"]) for e in ledger}, set(first))

    def test_dry_run_changes_nothing(self):
        before = len(self.fakes.devto_articles)
        out = self.sync({"posts/dry.md": story("Dry", platforms="[devto]")}, dry_run=True)
        r = out[("posts/dry.md", "devto")]
        self.assertEqual(r["action"], "create")
        self.assertEqual(r["preview"]["title"], "Dry")
        self.assertEqual(len(self.fakes.devto_articles), before)
        self.assertEqual(self.call("GET", f"/api/v1/ledger?source={self.id()}").json(), [])

    def test_idempotency_key_replays_instead_of_resending(self):
        body = {"source": self.id(), "files": [{"path": "p.md", "markdown": story("Key", platforms="[devto]")}]}
        first = self.call("POST", "/api/v1/stories:sync", json=body, headers={"Idempotency-Key": "k1"})
        replay = self.call("POST", "/api/v1/stories:sync", json=body, headers={"Idempotency-Key": "k1"})
        self.assertEqual(first.json(), replay.json())                 # "create" both times: a replay, not a re-run
        self.assertEqual(replay.headers.get("idempotent-replayed"), "true")
        other = self.call("POST", "/api/v1/stories:sync", json={**body, "ref": "x"}, headers={"Idempotency-Key": "k1"})
        self.assertEqual(other.status_code, 422)

    def test_idempotency_key_is_reserved_before_the_sync_runs(self):
        """Two identical requests at once: the second must not run the sync too, or save a different answer."""
        body = {"source": self.id(), "files": [{"path": "r.md", "markdown": story("Reserved", platforms="[devto]")}]}
        store = self.app.store
        user_id = store._one("SELECT user_id FROM api_tokens WHERE id = ?", self.pat["id"])["user_id"]
        before = len(self.fakes.devto_articles)
        # The first request holds the key and is still running.
        self.assertIsNone(store.reserve_idempotency_key(user_id, "k-race", "someone-else-running"))
        busy = self.call("POST", "/api/v1/stories:sync", json=body, headers={"Idempotency-Key": "k-race"})
        self.assertEqual(busy.status_code, 422)                       # a different request under the same key
        store.release_idempotency_key(user_id, "k-race")
        # Now the real first request: reserve it as the server would, and a twin arrives meanwhile.
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        self.assertIsNone(store.reserve_idempotency_key(user_id, "k-race", fingerprint))
        twin = self.call("POST", "/api/v1/stories:sync", json=body, headers={"Idempotency-Key": "k-race"})
        self.assertEqual((twin.status_code, twin.json()["error"]), (409, "idempotency_key_in_use"))
        self.assertEqual(len(self.fakes.devto_articles), before)      # the twin ran nothing
        store.release_idempotency_key(user_id, "k-race")              # the first one failed: the key is free again
        first = self.call("POST", "/api/v1/stories:sync", json=body, headers={"Idempotency-Key": "k-race"})
        replay = self.call("POST", "/api/v1/stories:sync", json=body, headers={"Idempotency-Key": "k-race"})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), replay.json())                 # "create", never the twin's "skipped"
        self.assertEqual(first.json()["results"][0]["action"], "create")
        self.assertEqual(len(self.fakes.devto_articles), before + 1)

    def test_unconfirmed_publish_is_checked_and_then_a_person_decides(self):
        before = len(self.fakes.devto_articles)
        self.fakes.devto_fail_next = 502                               # DEV fails after the request was sent
        r = self.sync({"posts/u.md": story("Uncertain", platforms="[devto]")})[("posts/u.md", "devto")]
        self.assertEqual(r["status"], "unconfirmed")
        self.assertEqual(len(self.fakes.devto_articles), before)
        # The next sync asks DEV first; the article isn't there, but the sync never creates it again (#226): the
        # person decides, and the result names the post and both calls.
        again = self.sync({"posts/u.md": story("Uncertain", platforms="[devto]")})[("posts/u.md", "devto")]
        self.assertEqual((again["action"], again["status"], again["post_id"]), ("skipped", "unconfirmed", r["post_id"]))
        self.assertIn(f"/api/v1/posts/{r['post_id']}/resend", again["message"])
        self.assertEqual(len(self.fakes.devto_articles), before)
        # A CI token can't decide (posts:write); the person's token can: one new post, and the file follows it.
        self.assertEqual(self.call("POST", f"/api/v1/posts/{r['post_id']}/resend").status_code, 403)
        resent = self.call("POST", f"/api/v1/posts/{r['post_id']}/resend", token=self.oauth)
        self.assertEqual(resent.status_code, 200, resent.text)
        self.assertEqual((resent.json()["status"], resent.json()["title"]), ("published", "Uncertain"))
        self.assertNotEqual(resent.json()["id"], r["post_id"])
        self.assertEqual(len(self.fakes.devto_articles), before + 1)
        self.assertEqual(self.call("POST", f"/api/v1/posts/{r['post_id']}/resend", token=self.oauth).status_code, 409)
        self.assertEqual(self.call("POST", "/api/v1/posts/pst_nope/resend", token=self.oauth).status_code, 404)
        settled = self.sync({"posts/u.md": story("Uncertain", platforms="[devto]")})[("posts/u.md", "devto")]
        self.assertEqual((settled["action"], settled["status"], settled["post_id"]),
                         ("unchanged", "published", resent.json()["id"]))
        self.assertEqual(len(self.fakes.devto_articles), before + 1)

    def test_scheduled_files_are_rescheduled_when_they_change(self):
        when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
        md = story("Later", platforms="[devto]", extra=f"publish_at: {when}\n")
        r = self.sync({"posts/later.md": md})[("posts/later.md", "devto")]
        self.assertEqual((r["action"], r["status"]), ("create", "scheduled"))
        r2 = self.sync({"posts/later.md": md.replace("Body.", "New body.")})[("posts/later.md", "devto")]
        self.assertEqual((r2["action"], r2["status"]), ("update", "scheduled"))
        self.assertNotEqual(r2["post_id"], r["post_id"])
        self.assertEqual(self.app.store.get_post(r["post_id"])["status"], "cancelled")

    def test_invalid_files_and_unconnected_platforms_are_reported(self):
        out = self.sync({"bad.md": "", "x.md": story("Xray", platforms="[devto, myspace]")})
        self.assertEqual(out[("bad.md", "*")]["action"], "failed")
        self.assertIn("myspace", out[("x.md", "*")]["message"])

    def test_cli_syncs_a_repository(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        (repo / "posts" / "2026").mkdir(parents=True)
        (repo / "posts" / "2026" / "cli.md").write_text(story("From the CLI", platforms="[devto]", body="![x](img/x.png)"))
        (repo / "README.txt").write_text("not markdown")
        summary = repo / "summary.md"
        outputs = repo / "outputs.txt"
        env = {"INKCAST_URL": self.base, "INKCAST_TOKEN": self.pat["token"], "GITHUB_REPOSITORY": "acme/blog",
               "GITHUB_SHA": "abc123", "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_OUTPUT": str(outputs),
               "GITHUB_SERVER_URL": "https://github.com"}
        old_env, old_cwd = dict(os.environ), os.getcwd()
        out = io.StringIO()
        try:
            os.environ.update(env)
            os.chdir(repo)
            with contextlib.redirect_stdout(out):
                code = sync_main(["posts/**/*.md", "--source", f"github.com/acme/blog-{self.id()}"])
            with contextlib.redirect_stdout(io.StringIO()):
                again = sync_main(["posts/**/*.md", "--source", f"github.com/acme/blog-{self.id()}"])
            os.environ.pop("INKCAST_TOKEN")
            with contextlib.redirect_stderr(io.StringIO()):
                missing_token = sync_main(["posts/**/*.md"])
        finally:
            os.chdir(old_cwd)
            os.environ.clear()
            os.environ.update(old_env)
        self.assertEqual((code, again, missing_token), (0, 0, 2))
        self.assertRegex(out.getvalue(), r"\+ create\s+devto\s+posts/2026/cli.md")
        self.assertIn("| + | `posts/2026/cli.md` | devto | create |", summary.read_text())
        self.assertRegex(outputs.read_text(), r"urls=https://dev.to/")
        sent = self.fakes.devto_articles[-1]["body_markdown"]
        self.assertRegex(sent, r"\(https://raw\.githubusercontent\.com/acme/blog-[^/]+/abc123/posts/2026/img/x\.png\)")

    def test_a_removed_file_cancels_its_pending_post_through_the_cli(self):
        """Issue #230: the push that deletes a file cancels the file's scheduled post; the CLI takes the commit before
        the push (as the action passes it) and the server cancels the post that came from that file."""
        repo = pathlib.Path(tempfile.mkdtemp())
        (repo / "posts").mkdir()
        when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
        (repo / "posts" / "gone.md").write_text(story("Gone", platforms="[devto]", extra=f"publish_at: {when}\n"))

        def git(*args: str) -> str:
            return subprocess.run(["git", "-c", "user.name=on-call", "-c", "user.email=on-call@example.com", *args],
                                  cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

        git("init", "-q", "-b", "main")
        git("add", ".")
        git("commit", "-q", "-m", "add")
        before = git("rev-parse", "HEAD")
        source = f"github.com/acme/blog-{self.id()}"
        old_env, old_cwd = dict(os.environ), os.getcwd()
        made, out = io.StringIO(), io.StringIO()
        try:
            os.environ.update({"INKCAST_URL": self.base, "INKCAST_TOKEN": self.pat["token"]})
            os.chdir(repo)
            with contextlib.redirect_stdout(made):
                created = sync_main(["posts/**/*.md", "--source", source, "--json"])
            git("rm", "-q", "posts/gone.md")
            git("commit", "-q", "-m", "remove")
            with contextlib.redirect_stdout(out):
                removed = sync_main(["--removed-since", before, "posts/**/*.md", "--source", source])
        finally:
            os.chdir(old_cwd)
            os.environ.clear()
            os.environ.update(old_env)
        self.assertEqual((created, removed), (0, 0))
        [first] = json.loads(made.getvalue())
        self.assertEqual((first["action"], first["status"]), ("create", "scheduled"))
        self.assertRegex(out.getvalue(), r"D removed\s+devto\s+posts/gone\.md\n    Post pst_\w+ was cancelled\.")
        self.assertEqual(self.app.store.get_post(first["post_id"])["status"], "cancelled")
        [entry] = self.call("GET", f"/api/v1/ledger?source={source}").json()
        self.assertEqual((entry["path"], entry["status"], entry["post_id"]), ("posts/gone.md", "cancelled", first["post_id"]))

    def test_a_sync_needs_files_or_removed_and_never_both_for_a_path(self):
        neither = self.call("POST", "/api/v1/stories:sync", json={"source": self.id()})
        self.assertEqual(neither.status_code, 422, neither.text)
        self.assertIn("files or removed is required", neither.json()["message"])
        both = self.call("POST", "/api/v1/stories:sync", json={
            "source": self.id(), "files": [{"path": "p.md", "markdown": story("Both")}], "removed": ["p.md"]})
        self.assertEqual(both.status_code, 422, both.text)
        self.assertIn("both synced and removed: p.md", both.json()["message"])
        self.assertEqual(self.call("GET", f"/api/v1/ledger?source={self.id()}").json(), [])
        never = self.sync({}, removed=["posts/never.md"])[("posts/never.md", "*")]
        self.assertEqual((never["action"], never["status"], never["message"]),
                         ("removed", None, "No post came from this file; nothing to cancel."))

    def test_token_browser_flow_shows_the_token_once(self):
        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                r = await c.get(f"{self.base}/tokens/new", params={"name": "laptop", "scopes": "stories:sync", "days": "30"})
                while r.status_code in (302, 303) and "/auth/consent" not in r.headers["location"]:
                    r = await c.get(urljoin(str(r.url), r.headers["location"]))
                confirm_url = urljoin(str(r.url), r.headers["location"])
                page = await c.get(confirm_url)
                fields = dict(re.findall(r'name=(\w+) value="([^"]*)"', page.text))
                done = await c.post(urljoin(confirm_url, "/auth/consent"),
                                    data={"pending": fields["pending"], "csrf": fields["csrf"], "decision": "approve"})
                replay = await c.post(urljoin(confirm_url, "/auth/consent"),
                                      data={"pending": fields["pending"], "csrf": fields["csrf"], "decision": "approve"})
                bad = await c.get(f"{self.base}/tokens/new", params={"name": "x", "scopes": "root"})
            return page, done, replay, bad
        page, done, replay, bad = asyncio.run(run())
        self.assertIn("Create a personal access token?", page.text)
        self.assertIn("stories:sync", page.text)
        token = re.search(r"(ink_pat_[A-Za-z0-9_\-]+)", done.text).group(1)
        self.assertEqual(done.headers["cache-control"], "no-store")
        self.assertEqual(self.call("GET", "/api/v1/ledger", token=token).status_code, 403)  # only stories:sync
        self.assertEqual(replay.status_code, 400)                                         # one use
        self.assertEqual(bad.status_code, 400)


class GitPublishingSplit(GitPublishing, SplitHarness):
    """The same, with every service in its own server behind the Gateway."""


del Harness, SplitHarness

if __name__ == "__main__":
    unittest.main()
