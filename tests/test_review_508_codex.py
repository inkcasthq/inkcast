# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #508 (Codex). The server now runs the request's patterns (``removed_globs``) over the source's ledger, so
their cost is the server's: a pattern of several ``**`` walks each split once, and each ledger path is matched against
the patterns once, however many directories the request names. The scope of a removed directory, and a link that
became a submodule (whose files are never collected, #476), are pinned."""

import asyncio
from unittest import mock

from inkcast import cli
from inkcast.libs import globs
from inkcast.libs.contracts.models import SyncRequest
from inkcast.services.publishing import sync as sync_module
from test_sync_error_after_post import at, markdown
from test_sync_removed_dirs_508 import SOURCE, ServerCase, commit, git, inside, new_repo


class GlobPatternCost(ServerCase):
    def test_a_pattern_of_several_globstars_does_bounded_work(self):
        """``**/`` seven times over a path twelve directories deep: 77,520 name comparisons before, under 2,000 now."""
        self.scheduled("posts/" + "/".join(["x"] * 12) + "/present.md")
        with mock.patch.object(globs, "_name", wraps=globs._name) as names:
            results = self.remove(["posts"], ["**/" * 7 + "absent.md"])
        self.assertEqual(results[0].path, "posts")
        self.assertLess(names.call_count, 2000)

    def test_fifty_nested_directories_match_each_path_once(self):
        path = "/".join(["a"] * 50) + "/present.md"
        self.scheduled(path)
        dirs = ["/".join(["a"] * n) for n in range(1, 51)]
        with mock.patch.object(sync_module, "glob_match", wraps=sync_module.glob_match) as matched:
            results = self.remove(dirs, [f"never{n}.md" for n in range(50)])
        self.assertEqual(len(results), 50)
        self.assertLessEqual(matched.call_count, 50)


class DirectoryScope(ServerCase):
    def test_prefix_case_equality_nested_name_source_and_user_are_isolated(self):
        target = self.scheduled("posts/a.md")
        excluded = [self.scheduled(path) for path in ("posts", "posts-old/a.md", "Posts/a.md", "archive/posts/a.md")]
        other_source = self.scheduled("posts/a.md", source=SOURCE + "-other")
        other_user = self.store.upsert_user("local:other-508", "other-508", None, None)["id"]
        self.store.save_credentials(other_user, self.platform.id, {"k": "v"}, {"id": "2", "name": "other"})
        other_req = SyncRequest(source=SOURCE, files=[{
            "path": "posts/a.md", "markdown": markdown(self.platform.id, at(2 * 24 * 3600))}])
        [other_result] = asyncio.run(self.git.sync(other_user, other_req)).results
        self.assertEqual(other_result.status, "scheduled")

        results = self.remove(["posts"], ["**"])
        self.assertEqual([r.path for r in results], ["posts/a.md"])
        self.assertEqual(self.store.get_post(target)["status"], "cancelled")
        for post_id in [*excluded, other_source, other_result.post_id]:
            self.assertEqual(self.store.get_post(post_id)["status"], "scheduled")


class ALinkThatBecameASubmodule(ServerCase):
    def test_is_a_directory_removal(self):
        """A submodule's files are never collected (#476), so what an older release synced below the link is removed."""
        missing = self.scheduled("guest/b.md")
        repo = new_repo(self)
        (repo / "real").mkdir()
        (repo / "real" / "a.md").write_text("# a\n")
        (repo / "guest").symlink_to("real")
        first = commit(repo, "linked guest")
        git(repo, "rm", "-q", "guest")
        module = repo.parent / "module"
        module.mkdir()
        git(module, "init", "-q", "-b", "main")
        (module / "a.md").write_text("# module\n")
        commit(module, "module")
        git(repo, "submodule", "add", "-q", str(module), "guest")
        commit(repo, "guest is now a submodule")
        with inside(repo):
            removals = cli.removals_since(first, ["guest/*.md"])
        self.assertEqual(removals.dirs, ["guest"])
        self.remove(removals.dirs, removals.globs)
        self.assertEqual(self.store.get_post(missing)["status"], "cancelled")
