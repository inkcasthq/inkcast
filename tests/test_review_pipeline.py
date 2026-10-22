# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of the publishing pipeline: content parsing, the scheduler, Git sync and the ledger.

Each test forces the input or the interleaving that used to break, and failed before the fix committed with it."""

import unittest

from inkcast.libs.content.story import StoryError, parse_story


class FrontMatterAliasBomb(unittest.TestCase):
    def test_yaml_aliases_in_front_matter_are_refused_not_expanded(self):
        """Nine aliases per level, seven levels: under 400 bytes that become a 25 MB tag (two more levels: 2 GB)
        as soon as anything turns the value into text. Any posts:read token can send it to /api/v1/preview."""
        names = "abcdefg"
        lines = ["a: &a [x, x, x, x, x, x, x, x, x]"]
        lines += [f"{n}: &{n} [{', '.join(['*' + prev] * 9)}]" for prev, n in zip(names, names[1:])]
        markdown = "---\n" + "\n".join(lines) + "\ntitle: Bomb\ntags: [*g]\n---\n\nBody.\n"
        self.assertLess(len(markdown), 400)
        with self.assertRaisesRegex(StoryError, "alias"):
            parse_story(markdown, known_platforms=["devto"])
        # Anchors alone (no alias refers to them) are harmless and still parse.
        story = parse_story("---\ntitle: &t Fine\ntags: [a]\n---\n\nBody.\n", known_platforms=["devto"])
        self.assertEqual((story.title, story.tags), ("Fine", ["a"]))


if __name__ == "__main__":
    unittest.main()
