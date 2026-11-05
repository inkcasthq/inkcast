# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of #508: the server matches a request's ``removed_globs`` against its ledger, so the glob walk runs on
input a ``stories:sync`` token chooses. The walk tried every split of the path at each ``**``, so a pattern of n
``**`` components against a path of k names took time growing as C(n + k, k): ``posts/`` and 40 ``**/`` (131
characters) against ``posts/2024/05/x/y/a.md`` took 2.5 s, 80 of them 60 s, on the event loop. The walk is now a table
of (component, name) pairs, each filled once."""

import asyncio
import fnmatch
import random
import re
import signal
import unittest

import inkcast.cli as cli
from inkcast.libs.contracts.models import SyncRequest
from inkcast.libs.globs import glob_match, glob_reaches_below
from test_sync_removed_dirs_508 import Sent, ServerCase, commit, git, new_repo

PATTERN = "posts/" + "**/" * 160 + "zz.md"   # 491 characters: within a pattern's 500
DEEP = "posts/" + "a/" * 240 + "a.md"         # 488 characters: within a path's 500


class _Deadline:
    """Fails the test, instead of hanging it, when the block outlives ``seconds``."""

    def __init__(self, seconds: float):
        self.seconds = seconds

    def __enter__(self):
        def expire(*_):
            raise TimeoutError(f"still matching after {self.seconds} s")
        self.old = signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, *exc):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.old)


class AnyPatternIsMatchedInTime(ServerCase):
    def test_the_server_answers_a_pattern_of_many_double_stars(self):
        a = self.scheduled("posts/2024/05/x/y/a.md")
        req = SyncRequest(source="github.com/acme/blog", removed_dirs=["posts"], removed_globs=[PATTERN] * 50)
        with _Deadline(5):
            [r] = asyncio.run(self.git.sync(self.user, req)).results
        self.assertEqual((r.path, r.message), ("posts", "No post came from below posts; nothing to cancel."))
        self.assertEqual(self.status(a), "scheduled")

    def test_a_path_below_many_directories_is_matched_once(self):
        """50 nested directories, each above the same deep entries: each entry is matched against the patterns once,
        not once per directory above it (50 x 50 walks of the longest pattern and path took 16 s for 5 entries)."""
        deep = [self.scheduled(DEEP[:-4] + f"{k}.md") for k in range(5)]
        dirs = ["posts"] + ["posts/" + "/".join(["a"] * k) for k in range(1, 50)]
        req = SyncRequest(source="github.com/acme/blog", removed_dirs=dirs,
                          removed_globs=["posts/" + "**/" * 160 + f"z{g}.md" for g in range(50)], dry_run=True)
        with _Deadline(5):
            results = asyncio.run(self.git.sync(self.user, req)).results
        self.assertEqual(len(results), 50)
        self.assertEqual({r.message.split(";")[0] for r in results}, {f"No post came from below {d}" for d in dirs})
        self.assertEqual({self.status(p) for p in deep}, {"scheduled"})

    def test_the_walk_is_bounded_for_the_longest_pattern_and_path(self):
        with _Deadline(5):
            self.assertFalse(glob_match(PATTERN, DEEP))
            self.assertTrue(glob_match("posts/" + "**/" * 160 + "a.md", DEEP))
            self.assertTrue(glob_reaches_below(PATTERN, "posts/" + "/".join(["a"] * 200)))
            self.assertFalse(glob_reaches_below("posts/" + "**/" * 160 + ".h/x", "posts/.g/" + "/".join(["a"] * 200)))


class APatternNamingAFileWithWildcardsInItsName(ServerCase):
    def test_is_matched_as_collect_falls_back_to_it(self):
        """``collect`` lists ``[pattern]`` when glob lists nothing and it names a file: ``posts/[x].md`` names the file
        ``[x].md``, which glob's ``[x]`` never lists. The server's ``or p == g`` is what cancels it; the #508 test of the
        fallback uses ``posts/a.md``, which ``glob_match`` lists anyway, so dropping ``or p == g`` failed no test."""
        a = self.scheduled("posts/[x].md")
        [r] = self.remove(["posts"], ["posts/[x].md"])
        self.assertEqual((r.path, r.status), ("posts/[x].md", "cancelled"))
        self.assertEqual(self.status(a), "cancelled")

class ADirectoryThisRunSyncsIsNotSent(unittest.TestCase):
    def test_a_deleted_link_whose_path_the_working_tree_holds(self):
        """``collect`` reads the working tree, ``--removed-since`` git's HEAD: a link deleted from git whose path a build
        step fills (here, ignored by git) has its files synced by this run, so a removal below it would cancel the posts
        this run just made (measured: '+ create posts/a.md', then 'D removed posts/a.md, Post ... was cancelled')."""
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
        self.assertEqual(code, 0)
        self.assertEqual([f["path"] for b in sent.bodies for f in b["files"]], ["posts/a.md"])
        self.assertEqual(sent.dirs(), [])
        self.assertIn("inkcast sync: not cancelled below posts: this run syncs files below it", err)


class AnyNumberOfPatternsIsSent(unittest.TestCase):
    def test_more_than_a_batch_of_patterns(self):
        """``removed_globs`` holds at most 50, as ``removed_dirs`` does; ``inkcast sync`` takes any number of patterns.
        With 51 the directory batch was a 422 and the run failed, whatever the directory."""
        repo = new_repo(self)
        (repo / "real").mkdir()
        (repo / "real" / "a.md").write_text("# a\n")
        (repo / "posts").symlink_to("real")
        first = commit(repo, "a linked directory")
        git(repo, "rm", "-q", "posts")
        commit(repo, "delete it")
        patterns = ["posts/*.md"] + [f"p{n}/*.md" for n in range(cli.BATCH)]
        sent = Sent()
        code, _ = sent.run(repo, ["--removed-since", first, *patterns, "--source", "s"])
        self.assertEqual(code, 0)
        for body in sent.bodies:
            SyncRequest.model_validate(body)   # what the server checks first: a 422 otherwise
        self.assertEqual(sorted(g for _, globs in sent.dirs() for g in globs), sorted(patterns))
        self.assertEqual({tuple(d) for d, _ in sent.dirs()}, {("posts",)})

# The walk as it was (e04f944e's ``cli.glob_match``), the reference the table must agree with.
_MAGIC = re.compile(r"[*?\[]")


def reference(parts: list[str], names: list[str]) -> bool:
    if not parts:
        return not names
    head, rest = parts[0], parts[1:]
    if head == "**":
        if not rest:
            return bool(names) and not any(name.startswith(".") for name in names)
        for taken in range(len(names) + 1):
            if reference(rest, names[taken:]):
                return True
            if taken < len(names) and names[taken].startswith("."):
                return False
        return False
    if not names:
        return False
    if _MAGIC.search(head):
        matched = (head.startswith(".") or not names[0].startswith(".")) and fnmatch.fnmatchcase(names[0], head)
    else:
        matched = names[0] == head
    return matched and reference(rest, names[1:])


class TheTableAgreesWithTheWalk(unittest.TestCase):
    def test_on_a_generated_corpus(self):
        rnd = random.Random(508)
        comps = ["a", "b.md", ".h", "*", "**", "*.md", "?", "[ab]", "[!a]*", ".*", "a**", "**b", "x", "[.]h", ""]
        names = ["a", "b.md", ".h", "x", "a.md", "ab", ".md", "[ab]", "**", ""]
        for _ in range(30000):
            pattern = "/".join(rnd.choice(comps) for _ in range(rnd.randint(1, 5)))
            path = "/".join(rnd.choice(names) for _ in range(rnd.randint(1, 5)))
            self.assertEqual(glob_match(pattern, path), reference(pattern.split("/"), path.split("/")), (pattern, path))
            directory = path.rsplit("/", 1)[0] if "/" in path else path
            below = any(reference(pattern.split("/"), f"{directory}/{t}".split("/"))
                        for t in ("a", "b.md", ".h", "x/a.md", "x/.h/a", "a/b/c.md"))
            if below:  # every tail tried lies below: a True from the reference is a True the table must give
                self.assertTrue(glob_reaches_below(pattern, directory), (pattern, directory))


if __name__ == "__main__":
    unittest.main()
