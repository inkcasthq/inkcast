# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #278 (#229): what the Git publishing guide says a re-run after a failed sync does.

docs/git-publishing.md says a post the failed run had already made "is followed, never made a second time: the re-run
reports its status". A post the platform refused is made: its row exists, as 'failed'. When the write of that outcome
is the one that fails (#229's third item), the request errors, and the re-run does not follow the refused post: the
entry is 'failed', so it makes a new post and sends the file again, as after any `failed` result. The guide must say
so, or an author reads the second post as the duplicate it promised never to make."""

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from inkcast.libs.platforms import PlatformError

from test_sync_error_after_post import SyncCase
from test_sync_error_leftovers import Locked

ROOT = Path(__file__).resolve().parent.parent


def rerun_bullets() -> list[str]:
    """The bullets under "If a sync fails part-way" in docs/git-publishing.md."""
    text = (ROOT / "docs/git-publishing.md").read_text(encoding="utf-8")
    start = text.index("If a sync fails part-way")
    end = text.index("\n## ", start)
    return [line for line in text[start:end].splitlines() if line.startswith("- ")]


class FindingTheGuideOnARefusedPost(SyncCase):
    def test_a_refused_post_is_made_again_by_the_rerun_and_the_guide_says_so(self):
        self.platform.error = PlatformError("Findable says: the title is too long.")
        locked = Locked(self.store.put_entry, lambda *a, **k: k.get("status") == "failed", times=1)
        with mock.patch.object(self.store, "put_entry", locked), self.assertRaises(sqlite3.OperationalError):
            self.sync()
        (refused,) = self.posts()
        self.assertEqual((refused["status"], self.entry()["status"]), ("failed", "failed"))

        self.platform.error = None
        again = self.sync()
        self.assertEqual(again.action, "create", "the re-run followed the refused post")
        self.assertEqual([p["status"] for p in self.posts()], ["failed", "published"])
        self.assertEqual(self.platform.sent, ["Body.", "Body."])

        (followed,) = [b for b in rerun_bullets() if "never made a second time" in b]
        self.assertIn("`failed`", followed, "the guide says a made post is never made again; a refused one is")


if __name__ == "__main__":
    unittest.main()
