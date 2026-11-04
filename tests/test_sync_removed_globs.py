# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #291: ``inkcast sync --removed-since`` reports a removed path only when ``collect``'s
``glob.glob(pattern, recursive=True)`` would have listed it while it existed. The regex it translated each pattern into
matched more: hidden files and directories (``.github/x.md`` under ``**/*.md``), ``**`` crossing directories inside a
component (``posts/**.md``), a class standing for ``/``; and a class holding a bracket (``[[]x[]]``) didn't compile.
Measured against ``glob.glob`` itself on a real tree, pattern by pattern, on every Python the tests run on."""

import contextlib
import glob
import os
import pathlib
import subprocess
import tempfile
import unittest
from typing import Iterator

from inkcast.cli import collect, glob_match, removed_since

# Every kind of name a repository has: hidden files and directories, nested directories, a bracket in a name, a name
# the single-character and class patterns below tell apart, and a sibling directory that is a prefix of ``posts``.
FILES = ("a.md", ".a.md", ".github/x.md", "post/f.md", "posts/a.md", "posts/ab.md", "posts/.d.md", "posts/.drafts/c.md",
         "posts/.drafts/deep/g.md", "posts/[x].md", "posts/x/y.md", "posts/sub/b.md", "posts/sub/.h.md", "posts/sub/deep/e.md",
         "posts/sub/deep/.i.md", "posts/2026/n.markdown", "posts/img/c.png")

PATTERNS = (
    "**/*.md", "posts/**/*.md", "posts/**.md", "posts/**", "**", "posts/*.md", "posts/*/*.md", "*/*.md", "./posts/*.md",
    "./**/*.md", "posts/?.md", "posts/??.md", "posts/[a-c].md", "posts/[!x]*.md", "posts/[!x]*/*.md", "posts[!x]a.md",
    "posts/[[]x[]].md", "posts/.*.md", "posts/.drafts/*.md", "posts/.d*/**/*.md", "**/.drafts/*.md", "posts/a.md",
    "posts/.d.md", "posts/.drafts/c.md", "posts/**/*.markdown", "**/*.markdown", "posts/**/**/*.md", "nowhere/**/*.md",
)


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


class GlobMatchAgreesWithGlobGlob(unittest.TestCase):
    """``glob_match(pattern, path)`` says exactly whether ``glob.glob(pattern, recursive=True)`` lists ``path``."""

    @classmethod
    def setUpClass(cls):
        cls.root = pathlib.Path(tempfile.mkdtemp())
        tree(cls.root)

    def test_every_pattern_lists_the_same_files(self):
        with inside(self.root):
            for pattern in PATTERNS:
                with self.subTest(pattern=pattern):
                    listed = sorted({str(pathlib.PurePosixPath(pathlib.Path(m).as_posix()))  # ``posts/**/**`` lists twice
                                     for m in glob.glob(pattern, recursive=True) if os.path.isfile(m)})
                    matched = sorted(f for f in FILES if glob_match(str(pathlib.PurePosixPath(pattern)), f))
                    self.assertEqual(matched, listed)

    def test_the_cases_the_review_measured(self):
        self.assertFalse(glob_match("**/*.md", ".github/x.md"))             # a hidden directory is never walked into
        self.assertFalse(glob_match("posts/**/*.md", "posts/.drafts/c.md"))
        self.assertFalse(glob_match("posts/*.md", "posts/.d.md"))           # nor a hidden file matched by a wildcard
        self.assertTrue(glob_match("posts/.*.md", "posts/.d.md"))           # unless the pattern asks for one
        self.assertTrue(glob_match("posts/.d.md", "posts/.d.md"))           # or names it
        self.assertFalse(glob_match("posts/**.md", "posts/sub/b.md"))       # ``**`` crosses directories only on its own
        self.assertTrue(glob_match("posts/**.md", "posts/ab.md"))
        self.assertFalse(glob_match("posts[!x]a.md", "posts/a.md"))        # a class never stands for ``/``
        self.assertTrue(glob_match("posts/[[]x[]].md", "posts/[x].md"))    # a bracket in a class compiles, and matches


class RemovedSinceReportsWhatCollectHadListed(unittest.TestCase):
    """End to end: the files ``collect`` listed at the first commit are exactly the ones ``--removed-since`` reports once
    the second commit has removed every file, for each pattern; a hidden file was never synced, so it is never
    reported removed."""

    def test_removed_paths_are_the_collected_ones(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "main")
        tree(repo)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "first")
        first = git(repo, "rev-parse", "HEAD")
        with inside(repo):
            collected = {pattern: collect([pattern]) for pattern in PATTERNS}
        for f in FILES:
            (repo / f).unlink()
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "second")
        with inside(repo):
            for pattern in PATTERNS:
                with self.subTest(pattern=pattern):
                    self.assertEqual(sorted(removed_since(first, [pattern])), sorted(collected[pattern]))
            self.assertEqual(removed_since(first, ["**/*.md"]), ["a.md", "post/f.md", "posts/[x].md", "posts/a.md", "posts/ab.md",
                                                                  "posts/sub/b.md", "posts/sub/deep/e.md", "posts/x/y.md"])


if __name__ == "__main__":
    unittest.main()
