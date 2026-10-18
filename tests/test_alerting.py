"""The alert catalogue and the SLOs: every alert has a runbook section, a known severity and a well-formed
expression; the burn-rate rules follow the multiwindow pattern; the generated PrometheusRule is current."""

import pathlib
import re
import subprocess
import sys
import unittest

import yaml

from inkcast.libs.alerting import ALERTS, BURN_WINDOWS, SEVERITIES, SLOS, all_alert_names, prometheus_rule, slo_rules

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "runbooks" / "alerts.md"


def anchors(markdown: str) -> set[str]:
    """GitHub-style anchors of every heading."""
    out = set()
    for line in markdown.splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            out.add(re.sub(r"[^a-z0-9 _-]", "", title).replace(" ", "-"))
    return out


class AlertCatalogue(unittest.TestCase):
    def test_every_alert_has_a_runbook_section_and_a_known_severity(self):
        known = anchors(RUNBOOK.read_text())
        names = all_alert_names()
        self.assertEqual(len(set(a.name for a in ALERTS)), len(ALERTS), "alert names are unique")
        for alert in ALERTS:
            with self.subTest(alert=alert.name):
                self.assertIn(alert.severity, SEVERITIES)
                self.assertIn(alert.runbook.rsplit("#", 1)[1], known, f"docs/runbooks/alerts.md needs a '## {alert.name}' section")
                self.assertTrue(alert.summary and alert.description and alert.service)
                self.assertRegex(alert.for_, r"^\d+[smh]$")
        for slo in SLOS:
            with self.subTest(slo=slo.name):
                self.assertIn(slo.runbook.rsplit("#", 1)[1], known, f"docs/runbooks/alerts.md needs a '### slo-{slo.name}' section")
                self.assertTrue(0.9 <= slo.objective < 1.0)
        self.assertTrue(all(n.startswith("Inkcast") for n in names))

    def test_expressions_are_balanced_and_name_registered_metrics(self):
        from inkcast.libs.config import Config
        from inkcast.libs.db import Store
        from inkcast.libs.runtime import build_metrics
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        registered = set(build_metrics(store, {}, Config(public_url="http://localhost:8787", secret_key="k" * 40,
                                                         database_path=":memory:"))._help)
        external = {"up", "kube_pod_container_status_restarts_total", "kubelet_volume_stats_available_bytes",
                    "kubelet_volume_stats_capacity_bytes"}
        exprs = [a.expr for a in ALERTS] + [s.good for s in SLOS] + [s.total for s in SLOS]
        for expr in exprs:
            with self.subTest(expr=expr):
                for open_, close in ("()", "{}", "[]"):
                    self.assertEqual(expr.count(open_), expr.count(close), "unbalanced brackets")
                for metric in re.findall(r"\b(inkcast_[a-z0-9_]+|up|kube_[a-z_]+|kubelet_[a-z_]+)\b(?=\s*[{\[\s)]|$)", expr):
                    base = re.sub(r"_(bucket|count|sum)$", "", metric)
                    self.assertTrue(base in registered or metric in external, f"{metric} is not a registered metric")

    def test_burn_rate_rules_follow_the_multiwindow_pattern(self):
        slo = SLOS[0]
        records, alerts = slo_rules(slo)
        self.assertEqual(len(records), 8)                                    # 7 windows + the objective
        self.assertEqual(len(alerts), len(BURN_WINDOWS))
        budget = 1 - slo.objective
        for (long, short, burn, severity), rule in zip(BURN_WINDOWS, alerts):
            self.assertIn(f"error_ratio_{long} > ({burn} * {budget:.6g})", rule["expr"])
            self.assertIn(f"error_ratio_{short} > ({burn} * {budget:.6g})", rule["expr"])
            self.assertEqual(rule["labels"]["severity"], severity)
            self.assertEqual(rule["labels"]["slo"], slo.name)
            self.assertIn("runbook_url", rule["annotations"])
        self.assertEqual([r["labels"]["severity"] for r in alerts], ["critical", "critical", "warning", "warning"])

    def test_prometheus_rule_is_well_formed_and_generated_files_are_current(self):
        doc = prometheus_rule()
        self.assertEqual(doc["kind"], "PrometheusRule")
        names = [r["alert"] for g in doc["spec"]["groups"] for r in g["rules"] if "alert" in r]
        self.assertEqual(len(names), len(ALERTS) + 4 * len(SLOS))
        for g in doc["spec"]["groups"]:
            for r in g["rules"]:
                if "alert" in r:
                    self.assertEqual(set(r["labels"]) >= {"severity", "team", "service"}, True)
                    self.assertTrue(r["annotations"]["runbook_url"].startswith("https://"))
        path = ROOT / "deploy" / "kustomize" / "components" / "prometheus-rules" / "prometheusrule.yaml"
        self.assertEqual(yaml.safe_load(path.read_text()), doc)
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-alerts.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
