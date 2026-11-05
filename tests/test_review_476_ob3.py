# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3 review of the #476 change: the submodules are read from the index of the repository git itself finds.

``_submodules`` ran ``git -C <top> ls-files``: ``-C`` changes directory first, so a relative ``GIT_DIR`` or
``GIT_WORK_TREE`` (git(1), ENVIRONMENT: relative to the directory git runs in) was read from the top instead of from
where the command runs. git then failed ("not a git repository: '../.git'"), ``_submodules`` gave nothing, and the
files of a submodule were collected again, silently. The index is now listed from where the command runs, with the top
as the pathspec: an absolute path, which git takes literally even under ``GIT_LITERAL_PATHSPECS`` (``:/`` does not)."""

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

from inkcast.cli import collect


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


class TheIndexIsTheOneGitFindsFromHere(unittest.TestCase):
    """``blog`` holds ``docs/d.md`` and the submodule ``guest``, whose working tree holds ``s.md``."""

    def setUp(self):
        base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        guest = base / "guest-src"
        guest.mkdir()
        git(guest, "init", "-q", "-b", "main")
        (guest / "s.md").write_text("# s\n")
        git(guest, "add", "-A")
        git(guest, "commit", "-q", "-m", "guest")
        self.repo = base / "blog"
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "docs" / "d.md").write_text("# d\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "submodule", "add", "-q", str(guest), "guest")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")

    def collect_from_docs(self, env: dict[str, str]) -> tuple[list[str], list[str]]:
        err = io.StringIO()
        with inside(self.repo / "docs"), mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            return collect(["../guest/*.md", "*.md"]), err.getvalue().splitlines()

    def test_a_relative_git_dir_and_work_tree(self):
        # on e47e9469: (['../guest/s.md', 'd.md'], []), ``git -C <top>`` reading '../.git' from the top
        self.assertEqual(self.collect_from_docs({"GIT_DIR": "../.git", "GIT_WORK_TREE": ".."}),
                         (["d.md"], ["inkcast sync: skipped ../guest/s.md: inside the submodule guest"]))

    def test_literal_pathspecs(self):
        """The guard on the pathspec chosen: ``:/`` would list nothing here, and the submodule be collected."""
        self.assertEqual(self.collect_from_docs({"GIT_LITERAL_PATHSPECS": "1"}),
                         (["d.md"], ["inkcast sync: skipped ../guest/s.md: inside the submodule guest"]))


if __name__ == "__main__":
    unittest.main()
