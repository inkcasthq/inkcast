# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""docs/screenshots.md: the curated pictures from the walkthrough (issue #78)."""

import importlib.util
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PAGE = REPO / "docs" / "screenshots.md"
IMAGES = REPO / "docs" / "images" / "showcase"


def showcase():
    spec = importlib.util.spec_from_file_location("showcase", REPO / "scripts" / "docs" / "showcase.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Screenshots(unittest.TestCase):
    def referenced(self) -> list[str]:
        return re.findall(r'<img src="images/showcase/([^"]+)"', PAGE.read_text())

    def test_every_picture_the_page_shows_exists(self):
        names = self.referenced()
        self.assertTrue(names)
        for name in names:
            self.assertTrue((IMAGES / name).is_file(), name)

    def test_no_picture_is_left_unreferenced(self):
        self.assertEqual(sorted(p.name for p in IMAGES.iterdir()), sorted(self.referenced()))

    def test_the_page_is_what_the_script_selects(self):
        self.assertEqual([shot[2] for shot in showcase().SHOTS], self.referenced())

    def test_the_page_names_the_run_it_came_from(self):
        self.assertRegex(PAGE.read_text(), r"From the run at commit `[0-9a-f]{12}` on \d{4}-\d{2}-\d{2}\.")

    def test_the_index_links_it(self):
        self.assertIn("(screenshots.md)", (REPO / "docs" / "README.md").read_text())
        self.assertIn("(docs/screenshots.md)", (REPO / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
