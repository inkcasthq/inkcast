# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The Git publishing guide's scope table lists every token scope the code grants (#282).

A reader picks a token's scopes from that table. A scope missing from it (``insights:read`` was) is one nobody asks
for, and the endpoints behind it answer 403 with no hint of what the token lacks."""

import re
import unittest
from pathlib import Path

from inkcast.libs.auth import SCOPES

ROOT = Path(__file__).resolve().parent.parent


def documented_scopes() -> set[str]:
    """The scopes named in the first column of the table under ``## 1. Get a token``."""
    text = (ROOT / "docs/git-publishing.md").read_text(encoding="utf-8")
    start = text.index("\n## 1. Get a token\n")
    section = text[start:text.index("\n## ", start + 1)]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    return {scope for row in rows for scope in re.findall(r"`([a-z]+:[a-z]+)`", row.split("|")[1])}


class TheScopeTableMatchesTheCode(unittest.TestCase):
    def test_every_scope_is_documented(self):
        self.assertEqual(set(SCOPES) - documented_scopes(), set(), "scopes missing from docs/git-publishing.md")

    def test_no_documented_scope_is_unknown(self):
        self.assertEqual(documented_scopes() - set(SCOPES), set(), "docs/git-publishing.md names scopes the code lacks")


if __name__ == "__main__":
    unittest.main()
