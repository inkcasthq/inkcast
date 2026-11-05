# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of #535 (PR #553): ``inkcast sync`` keeps a removed file only when the run syncs that very path.

The server keys a post by the exact path string (``ledger.path = ?``, no case folding, no Unicode normalisation), so a
removed path is kept only when ``collect`` lists the same string. A path that differs in case or in Unicode form, a file
of the same name in another directory, and a file still on disk that ``collect`` skips (a link, or below one) are other
paths: each is still sent as removed. The PR's tests pass with the comparison case-folded, NFC-normalised, by base name,
or replaced by "exists on disk"; these fail on each of those."""

import os
import unicodedata
import unittest

from test_sync_removed_dirs_508 import Sent, commit, git, new_repo
from test_sync_removed_but_present_535 import removed, synced

ARGS = ["posts/*.md", "--source", "s"]


class OnlyTheSamePathIsKept(unittest.TestCase):
    def test_a_removed_path_in_another_case_is_still_removed(self):
        """``posts/A.md`` deleted, ``posts/a.md`` written untracked: on a case-insensitive disk the same file, to the
        server another story, so ``posts/A.md``'s pending post is cancelled."""
        repo = new_repo(self)
        (repo / "posts").mkdir()
        (repo / "posts" / "A.md").write_text("# A\n")
        first = commit(repo, "A")
        git(repo, "rm", "-q", "posts/A.md")
        commit(repo, "A gone")
        (repo / "posts").mkdir(exist_ok=True)
        (repo / "posts" / "a.md").write_text("# a\n")
        self.assertEqual(os.listdir(repo / "posts"), ["a.md"])
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, *ARGS])
        self.assertEqual((code, synced(sent), removed(sent), err), (0, ["posts/a.md"], ["posts/A.md"], ""))

    def test_a_removed_path_in_another_unicode_form_is_still_removed(self):
        """Git holds the NFC name, the working tree the NFD one: two strings, two ledger entries."""
        nfc = unicodedata.normalize("NFC", "posts/café.md")
        nfd = unicodedata.normalize("NFD", nfc)
        repo = new_repo(self)
        (repo / "posts").mkdir()
        (repo / nfc).write_text("# nfc\n")
        first = commit(repo, "nfc")
        git(repo, "rm", "-q", "--", nfc)
        commit(repo, "nfc gone")
        (repo / "posts").mkdir(exist_ok=True)
        (repo / nfd).write_text("# nfd\n")
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, *ARGS])
        self.assertEqual((code, synced(sent), removed(sent), err), (0, [nfd], [nfc], ""))

    def test_a_removed_file_of_the_same_name_elsewhere_is_still_removed(self):
        repo = new_repo(self)
        for p in ("posts/a.md", "posts/old/a.md"):
            (repo / p).parent.mkdir(parents=True, exist_ok=True)
            (repo / p).write_text(f"# {p}\n")
        first = commit(repo, "two")
        git(repo, "rm", "-q", "posts/old/a.md")
        commit(repo, "old gone")
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, "posts/**/*.md", "--source", "s"])
        self.assertEqual((code, synced(sent), removed(sent), err), (0, ["posts/a.md"], ["posts/old/a.md"], ""))

    def test_a_file_that_became_a_link_is_still_removed(self):
        """The link is on disk, but ``collect`` skips it: the run doesn't sync it, so it removes it."""
        repo = new_repo(self)
        (repo / "posts").mkdir()
        (repo / "posts" / "a.md").write_text("# a\n")
        (repo / "posts" / "b.md").write_text("# b\n")
        first = commit(repo, "two")
        (repo / "posts" / "a.md").unlink()
        (repo / "posts" / "a.md").symlink_to("b.md")
        commit(repo, "a is a link")
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, *ARGS])
        self.assertEqual((code, synced(sent), removed(sent)), (0, ["posts/b.md"], ["posts/a.md"]))
        self.assertEqual(err, "inkcast sync: skipped posts/a.md: a symbolic link\n")

    def test_a_file_now_below_a_link_is_still_removed(self):
        repo = new_repo(self)
        for p in ("posts/a.md", "real/a.md"):
            (repo / p).parent.mkdir(parents=True, exist_ok=True)
            (repo / p).write_text(f"# {p}\n")
        first = commit(repo, "two")
        git(repo, "rm", "-q", "-r", "posts")
        (repo / "posts").symlink_to("real")
        commit(repo, "posts is a link")
        sent = Sent()
        code, err = sent.run(repo, ["--removed-since", first, *ARGS])
        self.assertEqual((code, synced(sent), removed(sent)), (0, [], ["posts/a.md"]))
        self.assertEqual(err, "inkcast sync: skipped posts/a.md: below the symbolic link posts\n")


if __name__ == "__main__":
    unittest.main()
