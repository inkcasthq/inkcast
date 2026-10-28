# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #230, the clients. ``inkcast sync --removed-since REV`` lists the markdown files its globs would have collected
that REV had and HEAD no longer has (``git diff --no-renames --diff-filter=D``, relative to the current directory as the
collected paths are) and sends them as ``removed``, in batches of their own. The GitHub action passes the commit before
the push as REV, nothing on the first push of a branch (all zeros) or when that commit can't be read, and a pull
request's base for the dry run; a shallow checkout fetches the base commit first. The action's step is run as GitHub
runs it (its ``run`` script under bash, with its ``env``), against a stand-in ``inkcast`` that records its arguments."""

import contextlib
import os
import pathlib
import subprocess
import tempfile
import unittest
from typing import Iterator

import yaml

from inkcast.cli import BATCH, CliError, build_requests, glob_pattern, removed_since

ROOT = pathlib.Path(__file__).resolve().parent.parent
ZEROS = "0" * 40
GLOB = "posts/**/*.md"


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=on-call", "-c", "user.email=on-call@example.com", *args], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def repo_with_a_removal() -> tuple[pathlib.Path, str]:
    """A repository whose second commit removes files of every kind (markdown under posts/, not markdown, outside
    posts/, a rename, an edit kept): the repository and its first commit."""
    repo = pathlib.Path(tempfile.mkdtemp())
    git(repo, "init", "-q", "-b", "main")
    for p in ("posts/a.md", "posts/2026/b.markdown", "posts/img/c.png", "notes/d.md", "posts/kept.md", "posts/renamed.md"):
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text(f"# {p}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "first")
    first = git(repo, "rev-parse", "HEAD")
    for p in ("posts/a.md", "posts/2026/b.markdown", "posts/img/c.png", "notes/d.md"):
        (repo / p).unlink()
    (repo / "posts/renamed.md").rename(repo / "posts/moved.md")
    (repo / "posts/kept.md").write_text("changed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    return repo, first


@contextlib.contextmanager
def inside(directory: pathlib.Path) -> Iterator[None]:
    old = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(old)


class TheGlobsMatchRemovedPaths(unittest.TestCase):
    def test_double_star_crosses_directories_and_single_star_does_not(self):
        deep = glob_pattern(GLOB)
        for path in ("posts/a.md", "posts/2026/b.md", "posts/x/y/z.md"):
            self.assertTrue(deep.match(path), path)
        for path in ("notes/d.md", "posts/a.markdown", "posts/a.md/extra", "xposts/a.md"):
            self.assertFalse(deep.match(path), path)
        flat = glob_pattern("posts/*.md")
        self.assertTrue(flat.match("posts/a.md"))
        self.assertFalse(flat.match("posts/2026/b.md"))
        self.assertTrue(glob_pattern("**/*.md").match("a.md"))
        self.assertTrue(glob_pattern("**/*.md").match("x/y/a.md"))

    def test_classes_single_characters_and_literal_paths(self):
        self.assertTrue(glob_pattern("posts/[ab]*.md").match("posts/alpha.md"))
        self.assertFalse(glob_pattern("posts/[ab]*.md").match("posts/charlie.md"))
        self.assertTrue(glob_pattern("posts/?.md").match("posts/a.md"))
        self.assertFalse(glob_pattern("posts/?.md").match("posts/ab.md"))
        self.assertTrue(glob_pattern("posts/a.md").match("posts/a.md"))
        self.assertFalse(glob_pattern("posts/a.md").match("posts/a.md.bak"))
        self.assertFalse(glob_pattern("posts/a+b.md").match("posts/aab.md"))  # a regex character is literal


class RemovedSince(unittest.TestCase):
    def test_lists_the_matching_markdown_files_the_revision_had(self):
        repo, first = repo_with_a_removal()
        with inside(repo):
            self.assertEqual(removed_since(first, [GLOB]), ["posts/a.md", "posts/renamed.md"])  # the rename's old path
            self.assertEqual(removed_since(first, ["posts/**/*.markdown"]), ["posts/2026/b.markdown"])
            self.assertEqual(removed_since(first, ["posts/**"]), ["posts/2026/b.markdown", "posts/a.md", "posts/renamed.md"])
            self.assertEqual(removed_since(first, ["notes/*.md", "posts/a.md"]), ["notes/d.md", "posts/a.md"])
            self.assertEqual(removed_since("HEAD", [GLOB]), [])

    def test_paths_are_relative_to_the_current_directory_as_collected_paths_are(self):
        repo, first = repo_with_a_removal()
        with inside(repo / "posts"):
            self.assertEqual(removed_since(first, ["**/*.md"]), ["a.md", "renamed.md"])

    def test_a_revision_git_cannot_read_is_an_error_not_an_empty_list(self):
        repo, _ = repo_with_a_removal()
        with inside(repo), self.assertRaisesRegex(CliError, "can't list the files removed since " + "f" * 40):
            removed_since("f" * 40, [GLOB])
        with inside(pathlib.Path(tempfile.mkdtemp())), self.assertRaisesRegex(CliError, "can't list the files removed"):
            removed_since("HEAD", [GLOB])


class RemovedPathsAreSentInBatchesOfTheirOwn(unittest.TestCase):
    def test_after_the_file_batches_fifty_at_a_time(self):
        removed = [f"posts/{i}.md" for i in range(BATCH + 1)]
        batches = build_requests([], "github.com/acme/blog", "abc", None, None, False, removed)
        self.assertEqual([(len(b["files"]), len(b["removed"])) for b in batches], [(0, BATCH), (0, 1)])
        self.assertEqual(batches[1], {"source": "github.com/acme/blog", "ref": "abc", "files": [], "removed": [removed[-1]],
                                      "platforms": None, "dry_run": False})
        self.assertEqual(build_requests([], "s", None, None, None, True), [])

    def test_a_removal_rides_no_file_batch(self):
        repo, _ = repo_with_a_removal()
        with inside(repo):
            batches = build_requests(["posts/kept.md"], "s", None, ["devto"], None, True, ["posts/a.md"])
        self.assertEqual([("removed" in b, [f["path"] for f in b["files"]]) for b in batches],
                         [(False, ["posts/kept.md"]), (True, [])])
        self.assertEqual((batches[1]["removed"], batches[1]["platforms"], batches[1]["dry_run"]), (["posts/a.md"], ["devto"], True))


class TheActionPassesTheCommitBeforeThePush(unittest.TestCase):
    """The sync step of ``action/action.yml``, run as GitHub runs it: under bash, with the step's ``env``."""

    @classmethod
    def setUpClass(cls):
        action = yaml.safe_load((ROOT / "action" / "action.yml").read_text())
        [cls.step] = [s for s in action["runs"]["steps"] if s.get("id") == "sync"]
        cls.bin = pathlib.Path(tempfile.mkdtemp())
        stub = cls.bin / "inkcast"
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_OUT"\n')
        stub.chmod(0o755)

    def test_the_step_takes_the_push_and_pull_request_commits_from_the_event(self):
        env = self.step["env"]
        self.assertEqual((env["PUSH_BEFORE"], env["PR_BASE"], env["EVENT"]),
                         ("${{ github.event.before }}", "${{ github.event.pull_request.base.sha }}", "${{ github.event_name }}"))

    def run_step(self, repo: pathlib.Path, event: str, before: str = "", base: str = "") -> tuple[list[str], str]:
        """The arguments the step gave ``inkcast``, and what it printed."""
        out = pathlib.Path(tempfile.mkdtemp())
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "ARGS_OUT": str(out / "args"),
               "RUNNER_TEMP": str(out), "INKCAST_URL": "https://inkcast.example.com", "INKCAST_TOKEN": "ink_pat_x_y",
               "PATHS": f"{GLOB} docs/*.md", "PLATFORMS": "", "DRY_RUN": "auto", "FAIL_ON_STALE": "false",
               "EVENT": event, "PUSH_BEFORE": before, "PR_BASE": base}
        done = subprocess.run(["bash", "-c", self.step["run"]], cwd=repo, env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        command, *args = (out / "args").read_text().splitlines()
        self.assertEqual(command, "sync")
        return args, done.stdout

    def test_a_push_passes_the_commit_before_it(self):
        repo, first = repo_with_a_removal()
        args, _ = self.run_step(repo, "push", before=first)
        self.assertEqual(args, ["--removed-since", first, GLOB, "docs/*.md"])

    def test_the_first_push_of_a_branch_passes_nothing(self):
        repo, _ = repo_with_a_removal()
        args, printed = self.run_step(repo, "push", before=ZEROS)
        self.assertEqual(args, [GLOB, "docs/*.md"])
        self.assertNotIn("::notice::", printed)

    def test_a_commit_that_cannot_be_read_passes_nothing_and_says_so(self):
        repo, _ = repo_with_a_removal()  # no origin to fetch from: as after a force push that dropped the commit
        args, printed = self.run_step(repo, "push", before="f" * 40)
        self.assertEqual(args, [GLOB, "docs/*.md"])
        self.assertIn("::notice::Inkcast: the commit before this push (" + "f" * 40 + ") can't be read", printed)

    def test_a_pull_request_passes_its_base_for_the_dry_run(self):
        repo, first = repo_with_a_removal()
        args, _ = self.run_step(repo, "pull_request", before="unused", base=first)
        self.assertEqual(args, ["--dry-run", "--removed-since", first, GLOB, "docs/*.md"])

    def test_any_other_event_passes_nothing(self):
        repo, first = repo_with_a_removal()
        args, _ = self.run_step(repo, "workflow_dispatch", before=first, base=first)
        self.assertEqual(args, [GLOB, "docs/*.md"])

    def test_a_shallow_checkout_fetches_the_commit_before_the_push(self):
        """actions/checkout clones one commit deep: the commit before the push isn't there until the step fetches it,
        and the diff then needs only the two trees."""
        upstream, first = repo_with_a_removal()
        git(upstream, "config", "uploadpack.allowReachableSHA1InWant", "true")  # as GitHub allows
        shallow = pathlib.Path(tempfile.mkdtemp()) / "checkout"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{upstream}", str(shallow)], check=True,
                       capture_output=True)
        self.assertEqual(subprocess.run(["git", "cat-file", "-e", first], cwd=shallow).returncode, 1, "not shallow")
        args, _ = self.run_step(shallow, "push", before=first)
        self.assertEqual(args, ["--removed-since", first, GLOB, "docs/*.md"])
        with inside(shallow):
            self.assertEqual(removed_since(first, [GLOB]), ["posts/a.md", "posts/renamed.md"])


if __name__ == "__main__":
    unittest.main()
