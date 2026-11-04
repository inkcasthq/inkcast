# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #290: a sync result that asks for a re-run (``skipped``: the file held by another sync, its post being sent,
its state changed under the sync) must get one. A re-run from the CLI or the action sends the same request body, so the
same ``Idempotency-Key`` (``sha256`` of the body); the server replayed the first answer for 24 hours, so the re-run
synced nothing, and a removal's pending post went out. Now a response with a ``skipped`` result isn't kept for replay:
the key is freed, the re-run runs, and the ledger keeps what the first run did. A response with nothing skipped is
replayed as before, and a repeat of it never publishes twice."""

import asyncio
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import httpx2

from inkcast.cli import sync_main
from test_e2e import Harness
from test_git_publishing import story


class ReRunAfterSkipped(Harness):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = cls.config.public_url
        h = {"Authorization": f"Bearer {cls('test_a_held_file_is_synced_by_the_rerun').access_token()}"}

        async def setup():
            async with httpx2.AsyncClient(timeout=30) as c:
                link = (await c.post(f"{cls.base}/api/v1/platforms/devto/connect-link", headers=h)).json()
                await c.post(link["url"], data={"api_key": "devto-key"})
                r = await c.post(f"{cls.base}/api/v1/tokens", headers=h, json={"name": "blog CI", "scopes": ["stories:sync"]})
                return r.json()
        cls.pat = asyncio.run(setup())
        cls.user_id = cls.app.store._one("SELECT user_id FROM api_tokens WHERE id = ?", cls.pat["id"])["user_id"]

    def post(self, body: dict, key: str) -> httpx2.Response:
        async def run():
            async with httpx2.AsyncClient(timeout=30) as c:
                return await c.post(f"{self.base}/api/v1/stories:sync", json=body,
                                    headers={"Authorization": f"Bearer {self.pat['token']}", "Idempotency-Key": key})
        return asyncio.run(run())

    def cli(self, repo: pathlib.Path, *args: str) -> list[dict]:
        """``inkcast sync`` run from ``repo`` against the server, twice the same way sends the same body and key."""
        old_cwd, old_env, out = os.getcwd(), dict(os.environ), io.StringIO()
        try:
            os.environ.update({"INKCAST_URL": self.base, "INKCAST_TOKEN": self.pat["token"]})
            os.chdir(repo)
            with contextlib.redirect_stdout(out):
                code = sync_main([*args, "--source", f"github.com/acme/blog-{self.id()}", "--ref", "abc123", "--json"])
        finally:
            os.chdir(old_cwd)
            os.environ.clear()
            os.environ.update(old_env)
        self.assertEqual(code, 0, out.getvalue())
        return json.loads(out.getvalue())

    def test_a_held_file_is_synced_by_the_rerun(self):
        """The issue's first case: the file is held by another sync (a failed run that couldn't record its error holds
        it for 10 minutes, docs/git-publishing.md); the person runs the sync again once the hold has cleared."""
        repo = pathlib.Path(tempfile.mkdtemp())
        (repo / "posts").mkdir()
        (repo / "posts" / "held.md").write_text(story("Held", platforms="[devto]"))
        source, before = f"github.com/acme/blog-{self.id()}", len(self.fakes.devto_articles)
        self.assertTrue(self.app.store.claim_new(self.user_id, source, "posts/held.md", "devto", "other-sync"))
        [first] = self.cli(repo, "posts/**/*.md")
        self.assertEqual((first["action"], first["message"]), ("skipped", "Another sync of this file is in progress."))
        self.app.store.drop_entry(self.user_id, source, "posts/held.md", "devto")   # the hold cleared
        [rerun] = self.cli(repo, "posts/**/*.md")
        self.assertEqual((rerun["action"], rerun["status"]), ("create", "published"), rerun)
        self.assertEqual(len(self.fakes.devto_articles), before + 1)
        [again] = self.cli(repo, "posts/**/*.md")                                   # and a third run changes nothing
        self.assertEqual((again["action"], len(self.fakes.devto_articles)), ("unchanged", before + 1))

    def test_a_removal_that_lost_its_race_cancels_the_post_on_the_rerun(self):
        """The issue's worst case: the removal found the post being sent and was skipped; no later push lists the path
        again (each push diffs only its own commits), so the re-run has to cancel it, or the pending post goes out."""
        when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
        source = f"github.com/acme/blog-{self.id()}"
        made = self.post({"source": source, "files": [{"path": "posts/gone.md",
                                                       "markdown": story("Gone", platforms="[devto]", extra=f"publish_at: {when}\n")}]},
                         key="made")
        [post] = made.json()["results"]
        self.assertEqual((post["action"], post["status"]), ("create", "scheduled"))
        self.app.store.update_post(post["post_id"], status="publishing")           # being sent as the removal arrives
        removal = {"source": source, "files": [], "removed": ["posts/gone.md"]}
        [first] = self.post(removal, key="removal").json()["results"]
        self.assertEqual((first["action"], first["status"]), ("skipped", "publishing"))
        self.app.store.update_post(post["post_id"], status="scheduled")            # the send yielded to a retry
        rerun = self.post(removal, key="removal")
        self.assertIsNone(rerun.headers.get("idempotent-replayed"), "the skipped answer was replayed")
        [second] = rerun.json()["results"]
        self.assertEqual((second["action"], second["status"]), ("removed", "cancelled"), second)
        self.assertEqual(self.app.store.get_post(post["post_id"])["status"], "cancelled")

    def test_a_response_with_nothing_skipped_is_still_replayed_and_never_published_twice(self):
        """What the key keeps doing: a repeat of the same request (a lost response, a retried batch) gets the first
        answer back and sends nothing again."""
        source, before = f"github.com/acme/blog-{self.id()}", len(self.fakes.devto_articles)
        body = {"source": source, "files": [{"path": "posts/once.md", "markdown": story("Once", platforms="[devto]")}]}
        first, replay = self.post(body, key="once"), self.post(body, key="once")
        self.assertEqual(first.json()["results"][0]["action"], "create")
        self.assertEqual((replay.json(), replay.headers.get("idempotent-replayed")), (first.json(), "true"))
        self.assertEqual(len(self.fakes.devto_articles), before + 1)

    def test_a_rerun_of_a_batch_with_a_skipped_file_publishes_nothing_twice(self):
        """A batch that made one post and skipped another isn't replayed; the re-run runs, and the ledger makes the
        post it made 'unchanged'. The idempotency the writer relies on is the ledger's, with or without a replay."""
        source, before = f"github.com/acme/blog-{self.id()}", len(self.fakes.devto_articles)
        body = {"source": source, "files": [
            {"path": "posts/made.md", "markdown": story("Made", platforms="[devto]")},
            {"path": "posts/elsewhere.md", "markdown": story("Elsewhere", platforms="[medium]")},   # not connected
        ]}
        first = {r["path"]: r["action"] for r in self.post(body, key="mixed").json()["results"]}
        self.assertEqual(first, {"posts/made.md": "create", "posts/elsewhere.md": "skipped"})
        rerun = self.post(body, key="mixed")
        self.assertIsNone(rerun.headers.get("idempotent-replayed"))
        self.assertEqual({r["path"]: r["action"] for r in rerun.json()["results"]},
                         {"posts/made.md": "unchanged", "posts/elsewhere.md": "skipped"})
        self.assertEqual(len(self.fakes.devto_articles), before + 1)


if __name__ == "__main__":
    unittest.main()
