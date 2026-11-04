# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #467 (#417): a story replaced by a symbolic link is reported removed.

``collect`` now lists regular files only, so a story whose file a later commit replaces with a link (``posts/a.md`` ->
``b.md``) is no longer synced. Git reports that change as a type change (``T``), not a deletion, so
``--diff-filter=D`` never names it either: the story is in neither ``files`` nor ``removed``, and its pending post goes
out. On main ``collect`` followed the link and kept syncing the path, so the gap is the PR's. The differential: what
``collect`` listed at the first commit and no longer lists at the second is what ``--removed-since`` reports, pattern
by pattern; a link that became a file is a new story, never a removal."""

import contextlib
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from typing import Iterator

from inkcast.cli import collect, removed_since

PATTERNS = ("**/*.md", "posts/*.md", "posts/a.md", "posts/c.md")


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


class AStoryReplacedByALinkIsRemoved(unittest.TestCase):
    def test_what_collect_stops_listing_is_reported_removed(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        git(repo, "init", "-q", "-b", "main")
        (repo / "posts").mkdir()
        for name in ("a.md", "b.md"):
            (repo / "posts" / name).write_text(f"# {name}\n")
        (repo / "posts" / "c.md").symlink_to("b.md")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "first")
        first = git(repo, "rev-parse", "HEAD")
        with inside(repo):
            before = {pattern: collect([pattern]) for pattern in PATTERNS}
        (repo / "posts" / "a.md").unlink()
        (repo / "posts" / "a.md").symlink_to("b.md")                    # a story becomes a link: git says T
        (repo / "posts" / "c.md").unlink()
        (repo / "posts" / "c.md").write_text("# c, a story now\n")      # a link becomes a story: a new one
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "second")
        self.assertEqual(git(repo, "diff", "--name-status", first, "HEAD").split(), ["T", "posts/a.md", "T", "posts/c.md"])
        with inside(repo):
            for pattern in PATTERNS:
                with self.subTest(pattern=pattern):
                    gone = sorted(set(before[pattern]) - set(collect([pattern])))
                    self.assertEqual(sorted(removed_since(first, [pattern])), gone)
            self.assertEqual(removed_since(first, ["posts/*.md"]), ["posts/a.md"])   # on the PR head: []


if __name__ == "__main__":
    unittest.main()
