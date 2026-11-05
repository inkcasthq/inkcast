# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #535: a file deleted from git but present in the working tree is synced, then cancelled, by the same run.

``collect`` reads the working tree and ``--removed-since`` reads git (``git diff <rev> HEAD``): a file a build step
regenerates, and ``.gitignore`` hides, is in both. ``build_requests`` sends the file batches first and the removed paths
after, so the run created the file's post and then cancelled it. Now a path the run syncs is never removed by the same
run: ``inkcast sync`` drops it from the removed files and names it on stderr, as #508 does for a removed directory the
run syncs files below. Both lists are in one form, relative to the current directory with ``/`` and no ``./``, which the
patterns written in other forms, and a run from a subdirectory, prove here."""

import pathlib
import unittest

from test_sync_removed_dirs_508 import Sent, commit, git, new_repo


def regenerated(repo: pathlib.Path, *paths: str) -> str:
    """``paths`` committed, then deleted from git and hidden by ``.gitignore``, then written again untracked, as a build
    step regenerates them: the commit that held them."""
    for p in paths:
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text(f"# {p}\n")
    first = commit(repo, "the files")
    git(repo, "rm", "-q", *paths)
    (repo / ".gitignore").write_text("".join(f"/{p}\n" for p in paths))
    commit(repo, "generated now")
    for p in paths:
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text(f"# {p}, generated\n")
    return first


def synced(sent: Sent) -> list[str]:
    return [f["path"] for b in sent.bodies for f in b["files"]]


def removed(sent: Sent) -> list[str]:
    return [p for b in sent.bodies for p in b.get("removed", [])]


class AFileThisRunSyncsIsNotRemoved(unittest.TestCase):
    def test_a_file_deleted_from_git_and_regenerated_untracked(self):
        repo = new_repo(self)
        first = regenerated(repo, "posts/a.md")
        self.assertEqual(git(repo, "status", "--porcelain", "--ignored"), "!! posts/")
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, "posts/*.md", "--source", "s", "--ref", "r"])
        self.assertEqual(code, 0)
        self.assertEqual(sent.bodies, [{"source": "s", "ref": "r", "platforms": None, "dry_run": False,
                                        "files": [{"path": "posts/a.md", "markdown": "# posts/a.md, generated\n",
                                                   "image_base_url": None}]}])
        self.assertEqual(err, "inkcast sync: not cancelled posts/a.md: this run syncs it\n")

    def test_in_every_form_a_pattern_and_the_directory_give_the_path(self):
        """``collect`` writes ``glob``'s matches through ``PurePosixPath``, and ``removals_since`` git's ``--relative``
        paths: the same string for a pattern with ``./`` or ``//``, a literal path, ``**``, and a run from below the
        top. A comparison of two forms would drop nothing, and the removal would be sent."""
        repo = new_repo(self)
        first = regenerated(repo, "posts/2026/a.md")
        for where, pattern, path in [(".", "posts/**/*.md", "posts/2026/a.md"),
                                     (".", "./posts/2026/*.md", "posts/2026/a.md"),
                                     (".", "posts//2026/*.md", "posts/2026/a.md"),
                                     (".", "posts/2026/a.md", "posts/2026/a.md"),
                                     (".", "**/*.md", "posts/2026/a.md"),
                                     ("posts", "**/*.md", "2026/a.md"),
                                     ("posts/2026", "./*.md", "a.md")]:
            with self.subTest(where=where, pattern=pattern):
                sent = Sent()
                code, err = sent.run(repo / where, ["--removed-since", first, pattern, "--source", "s"])
                self.assertEqual((code, synced(sent), removed(sent)), (0, [path], []))
                self.assertEqual(err, f"inkcast sync: not cancelled {path}: this run syncs it\n")

    def test_only_the_file_the_run_syncs_is_kept(self):
        repo = new_repo(self)
        (repo / "posts").mkdir()
        (repo / "posts" / "gone.md").write_text("# gone\n")
        first = regenerated(repo, "posts/a.md")
        git(repo, "rm", "-q", "posts/gone.md")
        commit(repo, "delete gone")
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, "posts/*.md", "--source", "s", "--dry-run"])
        self.assertEqual((code, synced(sent), removed(sent)), (0, ["posts/a.md"], ["posts/gone.md"]))
        self.assertEqual(err, "inkcast sync: not cancelled posts/a.md: this run syncs it\n")


class AFileGoneFromGitAndTheWorkingTreeIsStillRemoved(unittest.TestCase):
    def test_it_is_sent_as_removed(self):
        repo = new_repo(self)
        (repo / "posts").mkdir()
        (repo / "posts" / "a.md").write_text("# a\n")
        (repo / "posts" / "b.md").write_text("# b\n")
        first = commit(repo, "two")
        git(repo, "rm", "-q", "posts/a.md")
        commit(repo, "delete a")
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, "posts/*.md", "--source", "s", "--ref", "r"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(synced(sent), ["posts/b.md"])
        self.assertEqual(sent.bodies[-1], {"source": "s", "ref": "r", "files": [], "removed": ["posts/a.md"],
                                           "platforms": None, "dry_run": False})


class ADirectoryThisRunSyncsFilesBelowIsUnchanged(unittest.TestCase):
    def test_a_deleted_link_whose_path_the_working_tree_holds(self):
        """#508's case: the link is a directory removal, not a file one, and is named once, as #508 names it."""
        repo = new_repo(self)
        (repo / "real").mkdir()
        (repo / "real" / "a.md").write_text("# a\n")
        (repo / "posts").symlink_to("real")
        first = commit(repo, "a linked directory")
        git(repo, "rm", "-q", "posts")
        (repo / ".gitignore").write_text("posts/\n")
        commit(repo, "posts is generated now")
        (repo / "posts").mkdir()
        (repo / "posts" / "a.md").write_text("# generated\n")
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, "posts/*.md", "--source", "s"])
        self.assertEqual((code, synced(sent), removed(sent), sent.dirs()), (0, ["posts/a.md"], [], []))
        self.assertEqual(err, "inkcast sync: not cancelled below posts: this run syncs files below it\n")


if __name__ == "__main__":
    unittest.main()
