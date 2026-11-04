# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #291 (PR #406): ``inkcast sync --removed-since`` reports exactly the removed files ``collect`` listed at
the earlier commit, measured against ``glob.glob`` and ``collect`` themselves on a real tree and a real repository."""

import contextlib
import glob
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from typing import Iterator

from inkcast.cli import collect, glob_match, removed_since

FILES = ("a.md", "posts/a.md", "posts/b.md", "posts/sub/c.md")


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


class Repo(unittest.TestCase):
    """A repository holding ``FILES`` at ``first``; ``removed(patterns)`` removes them all and returns what
    ``collect`` listed at ``first`` and what ``removed_since(first)`` reports, pattern by pattern."""

    files = FILES

    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q", "-b", "main")
        for f in self.files:
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(f"# {f}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        self.first = git(self.repo, "rev-parse", "HEAD")

    def listed(self, pattern: str) -> list[str]:
        with inside(self.repo):
            return sorted(f for f in self.files if f in {str(pathlib.PurePosixPath(pathlib.Path(m).as_posix()))
                                                         for m in glob.glob(pattern, recursive=True)})

    def removed(self, patterns: list[str]) -> dict[str, tuple[list[str], list[str]]]:
        with inside(self.repo):
            collected = {p: sorted(collect([p])) for p in patterns}
        git(self.repo, "rm", "-rq", ".")
        git(self.repo, "commit", "-q", "-m", "second")
        with inside(self.repo):
            return {p: (collected[p], sorted(removed_since(self.first, [p]))) for p in patterns}


class AFinalDoubleStarIsNeverTheFileItself(Repo):
    """``posts/a.md/**`` walks into ``posts/a.md`` as a directory: glob lists nothing when it is a file. ``**`` as the
    last component stands for at least one name, the file at the end."""

    PATTERNS = ("posts/a.md/**", "*.md/**", "posts/**", "**", "posts/**/**", "posts/sub/**")

    def test_glob_match_agrees_with_glob(self):
        for pattern in self.PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertEqual(sorted(f for f in self.files if glob_match(pattern, f)), self.listed(pattern))

    def test_removed_since_reports_what_collect_listed(self):
        for pattern, (collected, reported) in self.removed(list(self.PATTERNS)).items():
            with self.subTest(pattern=pattern):
                self.assertEqual(reported, collected)


class APatternEndingInASlashListsNoFile(Repo):
    """``posts/**/`` and ``posts/*/`` list directories only, so ``collect`` sends no file for them; ``PurePosixPath``
    dropped the slash, and ``posts/**/`` reported every removed file under ``posts``."""

    PATTERNS = ("posts/**/", "posts/*/", "**/", "posts/sub/", "posts/*/.", "posts/**/*.md")

    def test_removed_since_reports_what_collect_listed(self):
        for pattern, (collected, reported) in self.removed(list(self.PATTERNS)).items():
            with self.subTest(pattern=pattern):
                self.assertEqual(reported, collected)


class AFileNamedByItsPatternIsReported(Repo):
    """``collect`` falls back to the pattern itself when glob lists nothing and the pattern names a file:
    ``posts/[x].md`` is a class to glob (``posts/x.md``), so the file ``posts/[x].md`` is synced through that fallback.
    Its removal is reported too, or its pending post goes out."""

    files = ("posts/[x].md", "posts/a*b.md", "posts/a.md")
    PATTERNS = ("posts/[x].md", "posts/a*b.md", "posts/*.md")

    def test_removed_since_reports_what_collect_listed(self):
        removed = self.removed(list(self.PATTERNS))
        self.assertEqual(removed["posts/[x].md"][0], ["posts/[x].md"])  # collected through the fallback
        for pattern, (collected, reported) in removed.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(reported, collected)


if __name__ == "__main__":
    unittest.main()
