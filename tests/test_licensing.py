"""Licensing hygiene: the licence, notices and CLA are present and every source file is marked."""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPDX = "SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0"


class Licensing(unittest.TestCase):
    def test_license_is_polyform_shield_with_required_notice(self):
        text = (ROOT / "LICENSE").read_text()
        self.assertRegex(text, r"(?m)^Required Notice: Copyright \d{4} ScalarOps Technologies LLC")
        self.assertRegex(text, r"(?m)^Licensor Line of Business: Inkcast")
        for section in ("# PolyForm Shield License 1.0.0", "## Noncompete", "## Patent Defense", "## Competition"):
            self.assertIn(section, text)

    def test_notice_carries_the_required_notice(self):
        self.assertIn("Required Notice: Copyright", (ROOT / "NOTICE").read_text())

    def test_every_source_file_is_marked(self):
        files = list((ROOT / "src" / "inkcast").rglob("*.py")) + [ROOT / "src/inkcast/static/swagger-init.js"]
        unmarked = [str(f.relative_to(ROOT)) for f in files if SPDX not in f.read_text()[:300]]
        self.assertEqual(unmarked, [])

    def test_package_metadata_declares_the_license(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('license = "LicenseRef-PolyForm-Shield-1.0.0"', pyproject)
        self.assertRegex(pyproject, r'license-files = \[.*"LICENSE".*"NOTICE".*\]')

    def test_contribution_terms_exist_and_grant_relicensing(self):
        cla = (ROOT / "legal" / "CLA-INDIVIDUAL.md").read_text()
        self.assertIn("ScalarOps Technologies LLC", cla)
        self.assertRegex(cla, r"(?i)relicense")
        self.assertRegex(cla, r"(?i)sell")
        for doc in ("CLA-ENTITY.md", "ADDITIONAL-PERMISSIONS.md", "COMMERCIAL-LICENSE.md", "README.md"):
            self.assertTrue((ROOT / "legal" / doc).exists(), doc)

    def test_licensing_model_covers_the_commercial_cases(self):
        text = (ROOT / "LICENSING.md").read_text()
        for phrase in ("Client publishing", "Monetise Inkcast", "Own content", "Commercial", "white-label"):
            self.assertIn(phrase, text)
        self.assertIn("No monetisation", (ROOT / "legal" / "ADDITIONAL-PERMISSIONS.md").read_text())

    def test_relative_links_in_legal_docs_resolve(self):
        broken = []
        for doc in [ROOT / "LICENSING.md", ROOT / "README.md", ROOT / "docs" / "README.md", *(ROOT / "legal").glob("*.md")]:
            for target in re.findall(r"\]\(([^)#:]+)(?:#[^)]*)?\)", doc.read_text()):
                if not (doc.parent / target).exists():
                    broken.append(f"{doc.relative_to(ROOT)} -> {target}")
        self.assertEqual(broken, [])
        for doc in ("CONTRIBUTING.md", "LICENSING.md", "TRADEMARKS.md", "SECURITY.md"):
            self.assertTrue((ROOT / doc).exists(), doc)

    def test_nothing_calls_inkcast_open_source(self):
        hits = []
        for f in ROOT.rglob("*.md"):
            if ".git" in f.parts or "vendor" in f.parts or "legal" in f.parts or f.name == "LICENSING.md":
                continue
            if re.search(r"(?i)\bopen[- ]source\b", f.read_text()):
                hits.append(str(f.relative_to(ROOT)))
        self.assertEqual(hits, [], "Inkcast is source-available; don't call it open source")


if __name__ == "__main__":
    unittest.main()
