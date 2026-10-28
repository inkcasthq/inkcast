# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #276 (#204, ADR-0017): the Git publishing guide states what happens to a post waiting for its retry.

docs/git-publishing.md tells the author that a time moved earlier re-schedules the post at the new time, and that a
removed time is published on the next sync. For a post the platform rate-limited, ADR-0017 does neither: the post is
changed in place and goes out no sooner than the platform's wait (test_sync_retry_wait_edit). An author who reads the
guide expects the post at a time it will not go out."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def guide_subsection(heading: str) -> str:
    """The text under a ``### heading`` of docs/git-publishing.md, up to the next heading."""
    text = (ROOT / "docs/git-publishing.md").read_text(encoding="utf-8")
    start = text.index(f"\n### {heading}\n")
    end = min(i for i in (text.find("\n## ", start + 1), text.find("\n### ", start + 1), len(text)) if i != -1)
    return text[start:end]


class TheGuideStatesTheRetryWait(unittest.TestCase):
    def test_a_changed_time_during_a_retry_wait_is_described(self):
        section = guide_subsection("How `publish_at` is read")
        self.assertIn("Retry-After", section, "the guide doesn't say a rate-limited post keeps the platform's wait")
        self.assertIn("adr/0017-a-post-waiting-for-its-retry-is-changed-in-place.md", section)


if __name__ == "__main__":
    unittest.main()
