# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #417: ``inkcast sync`` lists what git can report removed later.

``collect`` lists files from the filesystem (``glob.glob``) and ``--removed-since`` lists removals from git
(``git diff --diff-filter=D``). Where they disagreed, a synced file's removal was never reported and its pending post
went out: ``glob`` walks into a linked directory (git tracks the link alone, so ``linked/b.md`` is no path of git's),
and lists a directory named ``x.md`` as a file (reading it failed the whole run). Now ``collect`` lists regular files
only, reached through no symbolic link. On a case-insensitive filesystem (macOS) a pattern spelled in another case than
git's (``POSTS/*.md``) still keys the story under its own spelling: measured here and documented, not fixed; the
GitHub Action runs on Linux, where it can't happen."""

import contextlib
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from typing import Iterator

from inkcast.cli import collect, removed_since

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = ("a.md", "posts/a.md", "posts/sub/b.md", "x.md/c.md")   # ``x.md`` is a directory holding a story
LINKS = {"linked": "posts/sub", "posts/link.md": "a.md"}          # a linked directory, and a link to a file


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=on-call", "-c", "user.email=on-call@example.com", *args], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


@contextlib.contextmanager
def inside(directory: pathlib.Path) -> Iterator[None]:
    old = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(old)


def tree(root: pathlib.Path) -> None:
    for f in FILES:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(f"# {f}\n")
    for link, target in LINKS.items():
        (root / link).symlink_to(target)


class CollectListsRegularFilesUnderTheirOwnPath(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        tree(self.root)

    def test_a_directory_named_like_a_story_is_not_a_file(self):
        with inside(self.root):
            self.assertEqual(collect(["*.md"]), ["a.md"])                             # on main: ['a.md', 'x.md']
            self.assertEqual(collect(["x.md"]), [])
            self.assertEqual(collect(["x.md/*.md"]), ["x.md/c.md"])                  # the story inside it is a file

    def test_a_linked_directory_is_not_walked_into(self):
        with inside(self.root):
            self.assertEqual(collect(["**/*.md"]), ["a.md", "posts/a.md", "posts/sub/b.md", "x.md/c.md"])
            self.assertEqual(collect(["linked/*.md"]), [])                            # on main: ['linked/b.md']
            self.assertEqual(collect(["linked/b.md"]), [])                            # the fallback follows it no more

    def test_a_link_to_a_file_is_not_a_file(self):
        """Git tracks the link's target path as its content, never the story it points at."""
        with inside(self.root):
            self.assertEqual(collect(["posts/*.md"]), ["posts/a.md"])                 # on main: with 'posts/link.md'
            self.assertEqual(collect(["posts/link.md"]), [])


class RemovedSinceReportsWhatCollectHadListed(unittest.TestCase):
    """The review's differential test (test_sync_removed_globs.py), with a linked directory, a link to a file and a
    directory named like a story in the tree: what ``collect`` listed at the first commit is exactly what
    ``--removed-since`` reports once the second commit has removed everything, pattern by pattern."""

    PATTERNS = ("**/*.md", "posts/**/*.md", "*.md", "posts/*.md", "linked/*.md", "linked/b.md", "x.md", "x.md/*.md",
                "posts/link.md")

    def test_removed_paths_are_the_collected_ones(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        git(repo, "init", "-q", "-b", "main")
        tree(repo)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "first")
        first = git(repo, "rev-parse", "HEAD")
        self.assertEqual(git(repo, "ls-files", "-s", "linked").split()[0], "120000", "git tracks the link, as a link")
        with inside(repo):
            collected = {pattern: collect([pattern]) for pattern in self.PATTERNS}
        git(repo, "rm", "-rq", ".")
        git(repo, "commit", "-q", "-m", "second")
        with inside(repo):
            for pattern in self.PATTERNS:
                with self.subTest(pattern=pattern):
                    self.assertEqual(sorted(removed_since(first, [pattern])), sorted(collected[pattern]))
            self.assertEqual(removed_since(first, ["**/*.md"]), ["a.md", "posts/a.md", "posts/sub/b.md", "x.md/c.md"])


def case_insensitive(directory: pathlib.Path) -> bool:
    probe = directory / "Case.probe"
    probe.write_text("")
    try:
        return (directory / "case.PROBE").exists()
    finally:
        probe.unlink()


class TheCaseOfAPatternOnAnInsensitiveFilesystem(unittest.TestCase):
    """The documented behaviour, measured: ``glob`` keeps the pattern's spelling of a literal component, git keeps its
    own, and the two never meet in ``--removed-since``. Skipped where the filesystem tells the two apart (Linux)."""

    def test_the_docs_name_it(self):
        docs = (ROOT / "docs" / "git-publishing.md").read_text()
        for phrase in ("case-insensitive filesystem", "symbolic link", "regular files only"):
            self.assertTrue(phrase in docs, f"docs/git-publishing.md says nothing of {phrase!r}")

    def test_a_pattern_in_another_case_keys_the_story_under_its_own_spelling(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        if not case_insensitive(repo):
            self.skipTest("a case-sensitive filesystem: the pattern's spelling is the file's")
        git(repo, "init", "-q", "-b", "main")
        (repo / "posts").mkdir()
        (repo / "posts" / "a.md").write_text("# a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "first")
        first = git(repo, "rev-parse", "HEAD")
        with inside(repo):
            self.assertEqual(collect(["POSTS/*.md"]), ["POSTS/a.md"])         # the pattern's spelling, not git's
        git(repo, "rm", "-q", "posts/a.md")
        git(repo, "commit", "-q", "-m", "second")
        with inside(repo):
            self.assertEqual(removed_since(first, ["posts/*.md"]), ["posts/a.md"])
            self.assertEqual(removed_since(first, ["POSTS/*.md"]), [])         # never reported under the pattern's


if __name__ == "__main__":
    unittest.main()
