"""The operator dashboard follows the /dashboard standard: words on every panel, legend below, line width 1 with a
shade, one fixed palette colour per query, stated rate windows, only panel kinds that convert to Perses."""

import json
import pathlib
import subprocess
import sys
import unittest

from inkcast.libs.dashboards import BLUE, GREEN, IDENTITY, ORANGE, PANEL_KINDS, RATE, RED, YELLOW, platform_dashboard

ROOT = pathlib.Path(__file__).resolve().parents[1]
PALETTE = {*IDENTITY, GREEN, ORANGE}


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.doc = platform_dashboard()
        self.panels = [p for p in self.doc["panels"] if p["type"] != "row"]
        self.rows = [p for p in self.doc["panels"] if p["type"] == "row"]

    def test_sections_are_questions_and_panels_are_few(self):
        self.assertEqual([r["title"] for r in self.rows], ["Is Inkcast up?", "Is the queue moving?", "Are publishes succeeding?",
                                                           "Is the API healthy?", "Are events and the cache flowing?"])
        self.assertEqual(len(self.panels), 16)
        self.assertEqual([p["title"] for p in self.panels[:2]], ["Pods scraped", "Version and plug-ins"])

    def test_every_panel_has_words_a_unit_and_a_convertible_kind(self):
        for p in self.panels:
            with self.subTest(panel=p["title"]):
                self.assertIn(p["type"], PANEL_KINDS)
                self.assertGreater(len(p["description"]), 40, "the description says source, window, normal and empty")
                if p["type"] != "table":
                    self.assertTrue(p["fieldConfig"]["defaults"].get("unit"))
                for t in p["targets"]:
                    if "rate(" in t["expr"] or "increase(" in t["expr"]:
                        self.assertIn(f"[{RATE}]", t["expr"])
                        self.assertIn(RATE, p["description"], "the rate window is stated")

    def test_timeseries_follow_the_line_colour_and_legend_rules(self):
        for p in (p for p in self.panels if p["type"] == "timeseries"):
            with self.subTest(panel=p["title"]):
                self.assertEqual(p["options"]["legend"]["placement"], "bottom")
                self.assertEqual(p["fieldConfig"]["defaults"]["custom"]["lineWidth"], 1)
                self.assertEqual(p["fieldConfig"]["defaults"]["min"], 0)
                refs = [t["refId"] for t in p["targets"]]
                overrides = {o["matcher"]["options"]: o for o in p["fieldConfig"]["overrides"]}
                self.assertEqual(sorted(overrides), sorted(refs), "one fixed colour per query")
                stacked = p["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"] == "normal"
                for i, ref in enumerate(refs):
                    props = {x["id"]: x["value"] for x in overrides[ref]["properties"]}
                    self.assertIn(props["color"]["fixedColor"], PALETTE)
                    self.assertEqual(props["custom.fillOpacity"], 30 if stacked else 10)
                    if not stacked and i == 1:
                        self.assertEqual(props["custom.lineStyle"]["fill"], "dash")
                    if not stacked and i == 2:
                        self.assertEqual(props["custom.lineStyle"]["fill"], "dot")
                if not stacked:
                    self.assertEqual([t["refId"] for t in p["targets"]][:1], ["A"])
                    first = {x["id"]: x["value"] for x in overrides["A"]["properties"]}["color"]["fixedColor"]
                    self.assertEqual(first, BLUE, "the first identity is blue")
        outcomes = next(p for p in self.panels if p["title"].startswith("Publish outcomes"))
        colours = [{x["id"]: x["value"] for x in o["properties"]}["color"]["fixedColor"] for o in outcomes["fieldConfig"]["overrides"]]
        self.assertEqual(colours, [GREEN, RED, ORANGE, YELLOW])       # good, bad, warning, the third identity

    def test_layout_is_two_panels_per_row_and_generated_files_are_current(self):
        for p in self.panels:
            self.assertEqual((p["gridPos"]["w"], p["gridPos"]["h"]), (12, 8))
        self.assertEqual(len({p["id"] for p in self.doc["panels"]}), len(self.doc["panels"]))
        path = ROOT / "deploy" / "kustomize" / "components" / "dashboards" / "grafana-inkcast-platform.json"
        self.assertEqual(json.loads(path.read_text()), self.doc)
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-dashboards.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
