"""scripts/dev/sarif_report.py (issue #28): the local CodeQL run's report fails at CI's threshold and names the
source of a finding exactly (the related location's columns), so a fix can target the right expression."""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "dev" / "sarif_report.py"


def sarif(severity: str, level: str = "warning") -> dict:
    loc = {"physicalLocation": {"artifactLocation": {"uri": "app.py"}, "region": {"startLine": 2, "startColumn": 11,
                                                                                    "endColumn": 25}}}
    rule = {"id": "py/clear-text-logging-sensitive-data", "shortDescription": {"text": "Clear-text logging"},
            "properties": {"security-severity": severity}}
    return {"runs": [{"tool": {"driver": {"rules": [rule]}}, "results": [{
        "ruleId": rule["id"], "level": level, "message": {"text": "This expression logs [sensitive data](1)."},
        "locations": [loc], "relatedLocations": [{"id": 1, "message": {"text": "sensitive data (password)"}, **loc}]}]}]}


class SarifReport(unittest.TestCase):
    def run_report(self, data: dict):
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "app.py").write_text("import log\nlog.info(d[\"oauth_tokens\"])\n")
        (root / "r.sarif").write_text(json.dumps(data))
        return subprocess.run([sys.executable, str(SCRIPT), str(root / "r.sarif"), str(root)], capture_output=True,
                              text=True)

    def test_high_fails_and_names_the_source_expression(self):
        r = self.run_report(sarif("7.5"))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAILS THE CHECK", r.stdout)
        self.assertIn('`d["oauth_tokens"]`', r.stdout)

    def test_below_the_threshold_is_reported_but_passes(self):
        r = self.run_report(sarif("5.0"))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("1 result(s), 0 at the check's failure threshold", r.stdout)

    def test_an_error_level_result_fails_without_a_security_severity(self):
        self.assertEqual(self.run_report(sarif("", level="error")).returncode, 1)


if __name__ == "__main__":
    unittest.main()
