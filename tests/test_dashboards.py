"""The operator dashboard follows the /dashboard standard: words on every panel, legend below, line width 1 with a
shade, one fixed palette colour per query, stated rate windows, only panel kinds that convert to Perses."""

import json
import pathlib
import re
import subprocess
import sys
import unittest

from inkcast.libs.dashboards import (BLUE, GREEN, IDENTITY, NEUTRAL, ORANGE, PANEL_KINDS, RATE, RED, YELLOW,
                                     platform_dashboard)

ROOT = pathlib.Path(__file__).resolve().parents[1]
PALETTE = {*IDENTITY, GREEN, ORANGE, NEUTRAL}


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

    def test_no_data_is_never_painted_healthy_or_broken(self):
        """A threshold's base step is the colour of "No data": neutral, never green or red (group-sync-dashboard)."""
        stats = [p for p in self.panels if p["type"] == "stat"]
        self.assertGreaterEqual(len(stats), 4, "the check must cover the stat panels, not silently nothing")
        for p in stats:
            with self.subTest(panel=p["title"]):
                steps = p["fieldConfig"]["defaults"]["thresholds"]["steps"]
                self.assertEqual(steps[0], {"color": NEUTRAL, "value": None})
                self.assertEqual(steps[1]["value"], 0, "a real value starts at 0 with its own colour")
                self.assertEqual([s["value"] for s in steps[1:]], sorted(s["value"] for s in steps[1:]))
        pods = next(p for p in stats if p["title"] == "Pods scraped")["fieldConfig"]["defaults"]["thresholds"]["steps"]
        self.assertEqual([(s["value"], s["color"]) for s in pods[1:]], [(0, RED), (0.5, GREEN)], "0 pods is red, 1+ green")

    def test_tables_are_instant_with_named_columns(self):
        """A range query in a table gives one series per row, and Grafana shows a series picker, not a table."""
        for p in [p for p in self.panels if p["type"] == "table"]:
            with self.subTest(panel=p["title"]):
                for t in p["targets"]:
                    self.assertEqual((t["format"], t["instant"], t["range"]), ("table", True, False))
                organize = next(x["options"] for x in p["transformations"] if x["id"] == "organize")
                self.assertTrue(organize["excludeByName"]["Time"] and organize["excludeByName"]["Value"])
                self.assertTrue(organize["renameByName"], "every column has a header a person reads")

    def test_every_query_names_a_metric_inkcast_emits_or_a_recording_rule(self):
        """A panel over a metric nobody emits is a permanent 'No data' (borrowed from group-sync-dashboard)."""
        from inkcast.libs.alerting import prometheus_rule
        from inkcast.libs.config import Config
        from inkcast.libs.db import Store
        from inkcast.libs.runtime import build_metrics
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        registered = set(build_metrics(store, {}, Config(public_url="http://localhost:8787", secret_key="k" * 40,
                                                         database_path=":memory:"))._help)
        recorded = {r["record"] for g in prometheus_rule()["spec"]["groups"] for r in g["rules"] if "record" in r}
        checked = 0
        for p in self.panels:
            for t in p["targets"]:
                for name in re.findall(r"\b(inkcast_[a-z0-9_:]+|up)\b(?=\s*[{\[\s)]|$)", t["expr"]):
                    with self.subTest(panel=p["title"], metric=name):
                        base = re.sub(r"_(bucket|count|sum)$", "", name)
                        self.assertTrue(name == "up" or base in registered or name in recorded,
                                        f"{name} is neither a registered metric nor a recording rule")
                        checked += 1
        self.assertGreater(checked, 20, "the check must cover the dashboard, not silently nothing")

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
