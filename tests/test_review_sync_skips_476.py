# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #476, the rest of the OB3 review of PR #467 (#417): submodule files, silent skips, and a removed link.

- 467-a. ``collect`` listed the files inside a submodule (``glob`` walks into its working tree), while git tracks the
  submodule as one gitlink (mode 160000) with nothing of its own beneath it: their removal was never reported, and a
  story that became a submodule was both reported removed and synced. Now a path below a gitlink of the index is not
  collected. Without a repository (a run started from above it) there is no index, and nothing is skipped on this ground.
- 467-c. A match ``collect`` skips was dropped silently ("no markdown files matched.", exit 0, for a linked ``posts``).
  Now each is named on stderr, once, with the reason.
- 467-d. A link to a file, which main synced as a story of its own, was no longer reported once deleted: its pending
  post went out. Now a deleted link is a removal (at worst a "nothing to cancel" row); a link that became a file is
  still a new story, never a removal (test_review_collect_417_467.py)."""

import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from typing import Iterator
from unittest import mock

from inkcast.cli import collect, removed_since, sync_main

ROOT = pathlib.Path(__file__).resolve().parent.parent


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


def guest_repo(base: pathlib.Path) -> pathlib.Path:
    """A repository of its own, holding one story at its top, to be added as a submodule."""
    guest = base / "guest-src"
    guest.mkdir()
    git(guest, "init", "-q", "-b", "main")
    (guest / "s.md").write_text("# s\n")
    git(guest, "add", "-A")
    git(guest, "commit", "-q", "-m", "guest")
    return guest


class FilesInsideASubmoduleAreNotCollected(unittest.TestCase):
    """``blog`` holds ``a.md`` and ``docs/d.md``; its first commit holds ``guest/s.md`` as a story, its second replaces
    ``guest`` with a submodule whose working tree holds an ``s.md`` of its own."""

    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.repo = self.base / "blog"
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "guest").mkdir()
        for name in ("a.md", "docs/d.md", "guest/s.md"):
            (self.repo / name).write_text(f"# {name}\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        self.first = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "rm", "-rq", "guest")
        git(self.repo, "submodule", "add", "-q", str(guest_repo(self.base)), "guest")
        git(self.repo, "commit", "-q", "-m", "second")
        self.assertEqual(git(self.repo, "ls-files", "--stage", "guest").split()[0], "160000", "git tracks a gitlink")
        self.assertTrue((self.repo / "guest" / "s.md").is_file(), "the submodule's working tree holds a file")

    def test_a_path_below_a_gitlink_is_not_collected(self):
        with inside(self.repo), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(collect(["**/*.md"]), ["a.md", "docs/d.md"])      # on main: with 'guest/s.md'
            self.assertEqual(collect(["guest/s.md"]), [])
        with inside(self.repo / "docs"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(collect(["../guest/*.md", "*.md"]), ["d.md"])     # the index read from the top
        with inside(self.repo / "guest"):
            self.assertEqual(collect(["*.md"]), ["s.md"])                        # the remedy: the submodule's own sync

    def test_above_the_repository_there_is_no_index_and_nothing_is_skipped(self):
        """The decision recorded in docs/git-publishing.md: not "a directory holding .git", which would refuse this."""
        with inside(self.base):
            self.assertEqual(collect(["blog/guest/*.md"]), ["blog/guest/s.md"])

    def test_a_story_that_became_a_submodule_is_removed_and_no_longer_synced(self):
        """The differential of test_review_collect_417_467.py: what ``collect`` stopped listing is what is removed. On
        main ``guest/s.md`` was in both, so its new post was cancelled by the same run that made it."""
        with inside(self.repo), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(removed_since(self.first, ["**/*.md"]), ["guest/s.md"])
            self.assertNotIn("guest/s.md", collect(["**/*.md"]))

    def test_removing_the_submodule_reports_nothing(self):
        second = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "rm", "-q", "guest")
        git(self.repo, "commit", "-q", "-m", "third")
        with inside(self.repo):
            self.assertEqual(removed_since(second, ["**/*.md", "guest/*.md"]), [])

    def test_the_docs_record_the_decision(self):
        docs = (ROOT / "docs" / "git-publishing.md").read_text()
        for phrase in ("submodule", "160000", "named on stderr"):
            self.assertTrue(phrase in docs, f"docs/git-publishing.md says nothing of {phrase!r}")


class EachSkippedMatchIsNamed(unittest.TestCase):
    def test_the_cli_names_each_skip_once_with_its_reason(self):
        base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        repo = base / "blog"
        (repo / "drafts").mkdir(parents=True)
        (repo / "drafts" / "s.md").write_text("# s\n")
        (repo / "posts").symlink_to("drafts")                       # a linked directory
        (repo / "notes" / "x.md").mkdir(parents=True)               # a directory named like a story
        (repo / "notes" / "link.md").symlink_to("../drafts/s.md")   # a link to a story
        (repo / "notes" / "img.png").write_text("not markdown")     # never a match: not named
        git(repo, "init", "-q", "-b", "main")
        git(repo, "submodule", "add", "-q", str(guest_repo(base)), "guest")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "first")
        err = io.StringIO()
        env = {"INKCAST_URL": "https://inkcast.example.com", "INKCAST_TOKEN": "ink_pat_x_y"}
        with inside(repo), mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            code = sync_main(["posts/*.md", "posts/s.md", "notes/*", "guest/*.md"])
        self.assertEqual((code, err.getvalue().splitlines()), (0, [   # on main: guest/s.md collected, nothing named
            "inkcast sync: skipped posts/s.md: below the symbolic link posts",
            "inkcast sync: skipped notes/link.md: a symbolic link",
            "inkcast sync: skipped notes/x.md: not a regular file",
            "inkcast sync: skipped guest/s.md: inside the submodule guest",
            "inkcast sync: no markdown files matched.",
        ]))


class ADeletedLinkIsRemoved(unittest.TestCase):
    def test_a_deleted_link_to_a_file_is_reported_removed(self):
        """Main synced ``posts/link.md`` as a story (``collect`` followed the link); deleting it is the remedy."""
        repo = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        (repo / "posts").mkdir()
        (repo / "posts" / "a.md").write_text("# a\n")
        (repo / "posts" / "link.md").symlink_to("a.md")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "first")
        first = git(repo, "rev-parse", "HEAD")
        git(repo, "rm", "-q", "posts/link.md")
        git(repo, "commit", "-q", "-m", "second")
        with inside(repo):
            self.assertEqual(removed_since(first, ["posts/*.md"]), ["posts/link.md"])   # on main: []
            self.assertEqual(removed_since(first, ["posts/link.md"]), ["posts/link.md"])


if __name__ == "__main__":
    unittest.main()
