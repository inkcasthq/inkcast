# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #508: deleting a linked directory or a submodule cancels the pending posts an older release synced below it.

Before #417 ``collect`` followed a link to a directory, and before #476 it walked into a submodule's working tree: the
ledger can hold a scheduled post for ``posts/a.md`` (``posts`` a link to ``real/``) or ``guest/s.md`` (``guest`` a
submodule). Git reports the deletion as one entry (``D 120000 posts``, ``D 160000 guest``) with no path of its own below
it, so ``--removed-since`` listed nothing and those posts went out.

Now ``inkcast sync`` sends such a path as ``removed_dirs``, with its patterns as ``removed_globs``, in batches of their
own, and the server cancels the entries of that source below it that a pattern matches, through the removal path a
removed file takes. The server does it, under ``stories:sync``: the CI token is not widened to read the ledger. A link
with a markdown suffix stays a file removal (#476); a path that is a directory at HEAD is not sent, since its files are
synced as stories."""

import asyncio
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest import mock

import httpx2
from pydantic import ValidationError

import inkcast.cli as cli
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from test_e2e import Harness, connect_through_page
from test_split_mode import SplitHarness
from test_sync_error_after_post import SOURCE, SyncCase, at, markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = {"INKCAST_URL": "https://inkcast.example.com", "INKCAST_TOKEN": "ink_pat_x_y"}


def environment(**extra: str) -> dict[str, str]:
    """The process environment without GitHub Actions' variables (a CI run of these tests sets them: the ref, the job
    summary and the step outputs ``inkcast sync`` would write to), plus ``extra``."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}, **extra}


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=on-call", "-c", "user.email=on-call@example.com",
                           "-c", "protocol.file.allow=always", *args],  # a submodule added from a local path
                          cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@contextlib.contextmanager
def inside(directory: pathlib.Path) -> Iterator[None]:
    old = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(old)


def new_repo(test: unittest.TestCase) -> pathlib.Path:
    base = pathlib.Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, base, ignore_errors=True)
    repo = base / "blog"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    return repo


def commit(repo: pathlib.Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def linked_directory(repo: pathlib.Path, body: str = "# a\n") -> str:
    """``posts`` a link to ``real/`` holding ``a.md``; the commit holding it, then one that deletes the link."""
    (repo / "real").mkdir()
    (repo / "real" / "a.md").write_text(body)
    (repo / "posts").symlink_to("real")
    first = commit(repo, "a linked directory")
    git(repo, "rm", "-q", "posts")
    commit(repo, "delete the link")
    return first


def submodule(repo: pathlib.Path, body: str = "# s\n") -> str:
    """``guest`` a submodule holding ``s.md``; the commit holding it, then one that deletes it."""
    guest = repo.parent / "guest-src"
    guest.mkdir()
    git(guest, "init", "-q", "-b", "main")
    (guest / "s.md").write_text(body)
    commit(guest, "guest")
    git(repo, "submodule", "add", "-q", str(guest), "guest")
    first = commit(repo, "a submodule")
    git(repo, "rm", "-q", "guest")
    commit(repo, "delete the submodule")
    return first


class Sent:
    """A stand-in for the server: records each request body ``inkcast sync`` sends, answers each with ``answer``."""

    def __init__(self, answer: tuple[int, dict] = (200, {"results": []})):
        self.bodies: list[dict] = []
        self.answer = answer

    def client(self, base_url: str, token: str) -> httpx2.Client:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.bodies.append(json.loads(request.content))
            return httpx2.Response(self.answer[0], json=self.answer[1])
        return httpx2.Client(base_url=base_url, transport=httpx2.MockTransport(handle))

    def run(self, directory: pathlib.Path, argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with inside(directory), mock.patch.dict(os.environ, environment(**ENV), clear=True), \
                mock.patch.object(cli, "_client", self.client), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.sync_main(argv)
        return code, err.getvalue()

    def dirs(self) -> list[tuple[list[str], list[str]]]:
        return [(b["removed_dirs"], b["removed_globs"]) for b in self.bodies if b.get("removed_dirs")]


class TheCliSendsADeletedLinkOrSubmoduleAsADirectory(unittest.TestCase):
    def test_a_deleted_linked_directory(self):
        repo = new_repo(self)
        first = linked_directory(repo)
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, "posts/**/*.md", "--source", "s", "--ref", "r"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sent.bodies, [{"source": "s", "ref": "r", "files": [],
                                        "removed_dirs": ["posts"], "removed_globs": ["posts/**/*.md"],
                                        "platforms": None, "dry_run": False}])

    def test_a_deleted_submodule(self):
        repo = new_repo(self)
        first = submodule(repo)
        sent = Sent()
        code, _ = sent.run(repo, ["--removed-since", first, "guest/*.md", "--dry-run", "--source", "s"])
        self.assertEqual(code, 0)
        self.assertEqual(sent.dirs(), [(["guest"], ["guest/*.md"])])
        self.assertTrue(sent.bodies[0]["dry_run"])

    def test_the_batch_carries_no_removed_paths_so_an_older_server_refuses_it(self):
        """A server from before ``removed_dirs`` ignores the field, finds neither ``files`` nor ``removed``, and answers
        422 "files or removed is required": the run fails, naming it, and never reports the cancel as done."""
        repo = new_repo(self)
        first = linked_directory(repo)
        sent = Sent((422, {"error": "validation_error", "message": "files or removed is required"}))
        code, err = sent.run(repo, ["--removed-since", first, "posts/*.md", "--source", "s"])
        [body] = sent.bodies
        self.assertEqual((body["files"], "removed" in body), ([], False))
        self.assertEqual((code, err), (1, "inkcast sync: 422: files or removed is required\n"))

    def test_from_a_subdirectory_paths_are_relative_to_it(self):
        repo = new_repo(self)
        (repo / "site").mkdir()
        (repo / "site" / "real").mkdir()
        (repo / "site" / "real" / "a.md").write_text("# a\n")
        (repo / "site" / "posts").symlink_to("real")
        first = commit(repo, "a linked directory in site/")
        git(repo, "rm", "-q", "site/posts")
        commit(repo, "delete it")
        sent = Sent()
        sent.run(repo / "site", ["--removed-since", first, "posts/*.md", "--source", "s"])
        self.assertEqual(sent.dirs(), [(["posts"], ["posts/*.md"])])

    def test_many_directories_go_in_batches_of_their_own(self):
        repo = new_repo(self)
        (repo / "real").mkdir()
        (repo / "real" / "a.md").write_text("# a\n")
        for n in range(cli.BATCH + 1):
            (repo / f"l{n:02d}").symlink_to("real")
        first = commit(repo, "many links")
        git(repo, "rm", "-q", *[f"l{n:02d}" for n in range(cli.BATCH + 1)])
        commit(repo, "delete them")
        sent = Sent()
        sent.run(repo, ["--removed-since", first, "**/*.md", "--source", "s"])
        self.assertEqual([len(d) for d, _ in sent.dirs()], [cli.BATCH, 1])


class OtherDeletionsAreAsBefore(unittest.TestCase):
    def test_a_deleted_link_to_a_file_stays_a_file_removal(self):
        """#476: a link with a markdown suffix is a story path (a link to a file and a link to a directory can't be told
        apart from the deleted entry), so it is ``removed``, never a directory."""
        repo = new_repo(self)
        (repo / "posts").mkdir()
        (repo / "posts" / "a.md").write_text("# a\n")
        (repo / "posts" / "link.md").symlink_to("a.md")
        first = commit(repo, "a link to a file")
        git(repo, "rm", "-q", "posts/link.md")
        commit(repo, "delete it")
        sent = Sent()
        sent.run(repo, ["--removed-since", first, "posts/*.md", "--source", "s"])
        self.assertEqual([b.get("removed") for b in sent.bodies if not b["files"]], [["posts/link.md"]])
        self.assertEqual(sent.dirs(), [])

    def test_a_link_that_is_a_directory_now_is_not_sent_and_is_named(self):
        """Its files at HEAD are synced as stories: a removal below it would cancel the post this run just made."""
        repo = new_repo(self)
        (repo / "real").mkdir()
        (repo / "real" / "a.md").write_text("# a\n")
        (repo / "posts").symlink_to("real")
        first = commit(repo, "a linked directory")
        (repo / "posts").unlink()
        (repo / "real").rename(repo / "posts")
        commit(repo, "the link becomes the directory")
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, "posts/*.md", "--source", "s"])
        self.assertEqual(code, 0)
        self.assertEqual([f["path"] for b in sent.bodies for f in b["files"]], ["posts/a.md"])
        self.assertEqual(sent.dirs(), [])
        self.assertIn("inkcast sync: not cancelled below posts: it was a symbolic link and is a directory now", err)

    def test_a_link_still_there_is_no_removal(self):
        repo = new_repo(self)
        for d in ("one", "two"):
            (repo / d).mkdir()
            (repo / d / "a.md").write_text(f"# {d}\n")
        (repo / "posts").symlink_to("one")
        first = commit(repo, "a link")
        (repo / "posts").unlink()
        (repo / "posts").symlink_to("two")
        commit(repo, "it points elsewhere")
        sent = Sent()
        code, _ = sent.run(repo, ["--removed-since", first, "posts/*.md", "--source", "s"])
        self.assertEqual((code, sent.bodies), (0, []))

    def test_a_deleted_link_no_pattern_reaches_is_not_sent(self):
        repo = new_repo(self)
        (repo / "posts").mkdir()
        (repo / "posts" / "a.md").write_text("# a\n")
        (repo / "vendor").symlink_to("posts")
        (repo / ".hidden").symlink_to("posts")
        first = commit(repo, "links")
        git(repo, "rm", "-q", "vendor", ".hidden")
        commit(repo, "delete them")
        sent = Sent()
        sent.run(repo, ["--removed-since", first, "posts/**/*.md", "**/*.md", "--source", "s"])
        self.assertEqual(sent.dirs(), [(["vendor"], ["posts/**/*.md", "**/*.md"])])   # glob walks into no hidden one
        sent = Sent()
        sent.run(repo, ["--removed-since", first, "posts/**/*.md", "--source", "s"])
        self.assertEqual(sent.dirs(), [])

    def test_a_link_that_became_a_file_is_a_directory_removal(self):
        """``posts`` a link to a directory, then a regular file: nothing can be below it any more."""
        repo = new_repo(self)
        (repo / "real").mkdir()
        (repo / "real" / "a.md").write_text("# a\n")
        (repo / "posts").symlink_to("real")
        first = commit(repo, "a linked directory")
        (repo / "posts").unlink()
        (repo / "posts").write_text("not a directory\n")
        commit(repo, "a file now")
        sent = Sent()
        sent.run(repo, ["--removed-since", first, "posts/*.md", "--source", "s"])
        self.assertEqual(sent.dirs(), [(["posts"], ["posts/*.md"])])


class ServerCase(SyncCase):
    """Entries for ``posts/a.md`` and ``posts/b.md`` as an older release synced them through a link: scheduled posts."""

    def scheduled(self, path: str, source: str = SOURCE) -> str:
        req = SyncRequest(source=source, files=[SyncFile(path=path, markdown=markdown(self.platform.id, at(2 * 24 * 3600)))])
        (result,) = asyncio.run(self.git.sync(self.user, req)).results
        self.assertEqual(result.status, "scheduled")
        return result.post_id

    def remove(self, dirs: list[str], globs: list[str], dry_run: bool = False, removed: list[str] = ()):
        req = SyncRequest(source=SOURCE, removed=list(removed), removed_dirs=dirs, removed_globs=globs, dry_run=dry_run)
        return asyncio.run(self.git.sync(self.user, req)).results

    def status(self, post_id: str) -> str:
        return self.store.get_post(post_id)["status"]


class TheServerCancelsBelowARemovedDirectory(ServerCase):
    def test_each_entry_below_is_removed_as_a_removed_file_is(self):
        a, b = self.scheduled("posts/a.md"), self.scheduled("posts/b.md")
        results = self.remove(["posts"], ["posts/*.md"])
        self.assertEqual([(r.path, r.platform, r.action, r.status, r.post_id, r.message) for r in results], [
            ("posts/a.md", "findable", "removed", "cancelled", a, f"Post {a} was cancelled."),
            ("posts/b.md", "findable", "removed", "cancelled", b, f"Post {b} was cancelled.")])
        self.assertEqual((self.status(a), self.status(b)), ("cancelled", "cancelled"))
        entry = self.store.list_entries(self.user, SOURCE, "posts/a.md")[0]
        self.assertEqual((entry["status"], entry["content_hash"]), ("cancelled", ""))

    def test_a_dry_run_lists_them_and_cancels_nothing(self):
        a = self.scheduled("posts/a.md")
        [r] = self.remove(["posts"], ["posts/*.md"], dry_run=True)
        self.assertEqual((r.path, r.action, r.status, r.message), ("posts/a.md", "removed", "scheduled",
                                                                    f"Would cancel post {a} (scheduled)."))
        self.assertEqual(self.status(a), "scheduled")

    def test_an_entry_no_pattern_matches_is_untouched(self):
        deep = self.scheduled("posts/2026/a.md")
        [r] = self.remove(["posts"], ["posts/*.md"])
        self.assertEqual((r.path, r.platform, r.action, r.post_id), ("posts", "*", "removed", None))
        self.assertEqual(r.message, "No post came from below posts; nothing to cancel.")
        self.assertEqual(self.status(deep), "scheduled")

    def test_another_sources_entry_is_untouched(self):
        other = self.scheduled("posts/a.md", source="github.com/acme/other")
        [r] = self.remove(["posts"], ["posts/*.md"])
        self.assertEqual((r.path, r.platform), ("posts", "*"))
        self.assertEqual(self.status(other), "scheduled")

    def test_a_sibling_sharing_the_prefix_text_is_untouched(self):
        sibling = self.scheduled("posts-old/a.md")
        [r] = self.remove(["posts"], ["*/*.md"])
        self.assertEqual((r.path, r.platform), ("posts", "*"))
        self.assertEqual(self.status(sibling), "scheduled")

    def test_a_pattern_naming_the_file_is_matched_as_collect_falls_back_to_it(self):
        a = self.scheduled("posts/a.md")
        [r] = self.remove(["posts"], ["posts/a.md"])
        self.assertEqual((r.path, r.status), ("posts/a.md", "cancelled"))
        self.assertEqual(self.status(a), "cancelled")

    def test_a_path_removed_and_below_a_removed_directory_is_reported_once(self):
        a, b = self.scheduled("posts/a.md"), self.scheduled("posts/sub/b.md")
        results = self.remove(["posts", "posts/sub"], ["posts/**/*.md"], removed=["posts/a.md"])
        self.assertEqual([(r.path, r.status) for r in results], [("posts/a.md", "cancelled"), ("posts/sub/b.md", "cancelled")])
        self.assertEqual((self.status(a), self.status(b)), ("cancelled", "cancelled"))

    def test_the_ledger_is_not_listed_without_removed_dirs(self):
        self.scheduled("posts/a.md")
        with mock.patch.object(self.store, "list_entries", wraps=self.store.list_entries) as listed:
            asyncio.run(self.git.sync(self.user, SyncRequest(source=SOURCE, removed=["posts/a.md"])))
        self.assertEqual([c.args for c in listed.call_args_list], [(self.user, SOURCE, "posts/a.md")])


class TheRequestIsChecked(unittest.TestCase):
    def refused(self, **fields) -> str:
        with self.assertRaises(ValidationError) as caught:
            SyncRequest(source="s", **fields)
        return str(caught.exception)

    def test_a_directory_is_a_relative_normalised_path(self):
        for bad in ("/posts", "posts/", "./posts", "posts/.", "../posts", "posts/../x", "a//b", ".", "..", "a\0b"):
            with self.subTest(bad=bad):
                self.assertIn("isn't a relative, normalised path", self.refused(removed_dirs=[bad], removed_globs=["*"]))
        self.assertIn("at least 1 character", self.refused(removed_dirs=[""], removed_globs=["*"]))
        SyncRequest(source="s", removed_dirs=["posts", ".github/x", "a b/c..d"], removed_globs=["**/*.md"])

    def test_the_patterns_go_with_the_directories(self):
        self.assertIn("removed_dirs needs removed_globs", self.refused(removed_dirs=["posts"]))
        self.assertIn("removed_globs is only for removed_dirs", self.refused(removed=["a.md"], removed_globs=["*.md"]))
        self.assertIn("at least 1 character", self.refused(removed_dirs=["posts"], removed_globs=[""]))

    def test_the_same_limits_as_removed(self):
        many = [f"d{n}" for n in range(51)]
        self.assertIn("at most 50 items", self.refused(removed_dirs=many, removed_globs=["*"]))
        self.assertIn("at most 50 items", self.refused(removed_dirs=["d"], removed_globs=["*"] * 51))
        self.assertIn("at most 500 characters", self.refused(removed_dirs=["d" * 501], removed_globs=["*"]))

    def test_a_file_synced_below_a_removed_directory_is_refused(self):
        refused = self.refused(files=[SyncFile(path="posts/a.md", markdown="x")], removed_dirs=["posts"],
                               removed_globs=["posts/*.md"])
        self.assertIn("a path can't be both synced and below a removed directory: posts/a.md", refused)


def story(title: str, when: str) -> str:
    return f"---\ntitle: {title}\nplatforms: [devto]\npublish_at: {when}\n---\n\nBody.\n"


class EndToEnd(Harness):
    """The CLI against the server, with a token holding ``stories:sync`` only, as docs/git-publishing.md makes one."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = cls.config.public_url
        oauth = cls("test_a_deleted_linked_directory_cancels_its_posts").access_token()
        h = {"Authorization": f"Bearer {oauth}"}

        async def setup():
            async with httpx2.AsyncClient(timeout=30) as c:
                link = (await c.post(f"{cls.base}/api/v1/platforms/devto/connect-link", headers=h)).json()
                await connect_through_page(c, link["url"], {"api_key": "devto-key"})
                return (await c.post(f"{cls.base}/api/v1/tokens", headers=h,
                                     json={"name": "blog CI", "scopes": ["stories:sync"]})).json()
        cls.pat = asyncio.run(setup())

    def call(self, method: str, path: str, **kw) -> httpx2.Response:
        async def run():
            async with httpx2.AsyncClient(timeout=30) as c:
                return await c.request(method, f"{self.base}{path}",
                                       headers={"Authorization": f"Bearer {self.pat['token']}"}, **kw)
        return asyncio.run(run())

    def synced(self, path: str, source: str) -> str:
        """``path`` synced as a release before #417 or #476 synced it: a scheduled post on DEV."""
        when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
        r = self.call("POST", "/api/v1/stories:sync", json={"source": source, "files": [
            {"path": path, "markdown": story(f"{path} {source}", when)}]})
        self.assertEqual(r.status_code, 200, r.text)
        [result] = r.json()["results"]
        self.assertEqual((result["action"], result["status"]), ("create", "scheduled"))
        return result["post_id"]

    def cli(self, repo: pathlib.Path, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = environment(INKCAST_URL=self.base, INKCAST_TOKEN=self.pat["token"])
        with inside(repo), mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.sync_main(argv)
        return code, out.getvalue(), err.getvalue()

    def status(self, post_id: str) -> str:
        return self.app.store.get_post(post_id)["status"]

    def test_the_token_holds_stories_sync_only(self):
        self.assertEqual(self.pat["scopes"], ["stories:sync"])
        self.assertEqual(self.call("GET", "/api/v1/ledger").status_code, 403)

    def test_a_deleted_linked_directory_cancels_its_posts(self):
        source = f"github.com/acme/{self.id()}"
        a = self.synced("posts/a.md", source)
        sibling = self.synced("posts-old/a.md", source)
        elsewhere = self.synced("posts/a.md", source + "-other")
        repo = new_repo(self)
        first = linked_directory(repo)
        code, out, err = self.cli(repo, ["--removed-since", first, "posts/**/*.md", "--source", source])
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, rf"^D removed\s+devto\s+posts/a\.md\n    Post {a} was cancelled\.$")
        self.assertEqual([self.status(p) for p in (a, sibling, elsewhere)], ["cancelled", "scheduled", "scheduled"])

    def test_a_deleted_submodule_cancels_its_posts(self):
        source = f"github.com/acme/{self.id()}"
        s = self.synced("guest/s.md", source)
        repo = new_repo(self)
        first = submodule(repo)
        code, out, err = self.cli(repo, ["--removed-since", first, "guest/*.md", "--source", source])
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, rf"^D removed\s+devto\s+guest/s\.md\n    Post {s} was cancelled\.$")
        self.assertEqual(self.status(s), "cancelled")

    def test_a_dry_run_lists_them_and_cancels_nothing(self):
        source = f"github.com/acme/{self.id()}"
        a = self.synced("posts/a.md", source)
        repo = new_repo(self)
        first = linked_directory(repo)
        code, out, _ = self.cli(repo, ["--removed-since", first, "--dry-run", "posts/*.md", "--source", source])
        self.assertEqual(code, 0)
        self.assertRegex(out, rf"^D removed\s+devto\s+posts/a\.md\n    Would cancel post {a} \(scheduled\)\.$")
        self.assertEqual(self.status(a), "scheduled")

    def test_a_pattern_that_does_not_match_removes_nothing(self):
        source = f"github.com/acme/{self.id()}"
        deep = self.synced("posts/2026/a.md", source)
        repo = new_repo(self)
        first = linked_directory(repo)
        code, out, _ = self.cli(repo, ["--removed-since", first, "posts/*.md", "--source", source])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^D removed\s+\*\s+posts\n    No post came from below posts; nothing to cancel\.$")
        self.assertEqual(self.status(deep), "scheduled")

    def test_each_refusal_is_a_422(self):
        source = f"github.com/acme/{self.id()}"
        for body, said in (
                ({"removed_dirs": ["/posts"], "removed_globs": ["*"]}, "isn't a relative, normalised path"),
                ({"removed_dirs": ["../posts"], "removed_globs": ["*"]}, "isn't a relative, normalised path"),
                ({"removed_dirs": ["posts"]}, "removed_dirs needs removed_globs"),
                ({"removed_dirs": ["posts"], "removed_globs": [""]}, "at least 1 character"),
                ({"removed": ["a.md"], "removed_globs": ["*"]}, "removed_globs is only for removed_dirs"),
                ({"removed_dirs": [f"d{n}" for n in range(51)], "removed_globs": ["*"]}, "at most 50 items")):
            with self.subTest(body=body):
                r = self.call("POST", "/api/v1/stories:sync", json={"source": source, **body})
                self.assertEqual(r.status_code, 422, r.text)
                self.assertIn(said, r.json()["message"])
        ok = self.call("POST", "/api/v1/stories:sync", json={"source": source, "removed_dirs": ["posts"],
                                                               "removed_globs": ["posts/*.md"]})
        self.assertEqual(ok.status_code, 200, ok.text)   # da780ddc: 422 "files or removed is required"

    def test_the_openapi_document_describes_the_fields(self):
        r = httpx2.get(f"{self.base}/api/openapi.json", timeout=30)
        props = r.json()["components"]["schemas"]["SyncRequest"]["properties"]
        self.assertIn("removed_dirs", props)
        self.assertIn("removed_globs", props)


class EndToEndSplit(EndToEnd, SplitHarness):
    """The same, with every service in its own server behind the Gateway."""


class TheDocsSayIt(unittest.TestCase):
    def test_removing_a_file_names_linked_directories_and_submodules(self):
        docs = (ROOT / "docs" / "git-publishing.md").read_text()
        section = docs.split("### Removing a file", 1)[1].split("\n## ", 1)[0]
        for phrase in ("linked directory", "submodule", "removed_dirs", "#508"):
            self.assertIn(phrase, section)


if __name__ == "__main__":
    unittest.main()
