"""Who may reach Inkcast's ports (issue #21): the metrics port, which also serves /secrets (names and sources, never
values), admits Prometheus pods only, by namespace AND pod; and every scraped series names its Inkcast service."""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "kustomize"


def load(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text())


class MetricsPort(unittest.TestCase):
    def test_the_base_policy_does_not_open_the_metrics_port(self):
        base = load("base/networkpolicy.yaml")
        ports = [p["port"] for rule in base["spec"]["ingress"] for p in rule.get("ports", [])]
        self.assertNotIn(9090, ports, "the base's label transform would rewrite a pod selector here; see #21")

    def test_only_prometheus_pods_in_the_monitoring_namespace_reach_it(self):
        policy = load("components/servicemonitor/networkpolicy.yaml")
        (rule,) = policy["spec"]["ingress"]
        (peer,) = rule["from"]                       # one element: namespace AND pod must both match
        self.assertIn("namespaceSelector", peer)
        self.assertEqual(peer["podSelector"]["matchLabels"], {"app.kubernetes.io/name": "prometheus"})
        self.assertEqual([p["port"] for p in rule["ports"]], [9090])
        self.assertIn("networkpolicy.yaml", load("components/servicemonitor/kustomization.yaml")["resources"])

    def test_every_overlay_with_prometheus_points_the_policy_at_its_monitoring_namespace(self):
        for overlay in ("kubernetes", "openshift"):
            with self.subTest(overlay=overlay):
                k = load(f"overlays/{overlay}/kustomization.yaml")
                self.assertIn("../../components/servicemonitor", k["components"])
                targets = [p["target"].get("name") for p in k.get("patches", [])]
                self.assertIn("inkcast-metrics", targets)

    def test_series_name_their_inkcast_service(self):
        (endpoint,) = load("components/servicemonitor/servicemonitor.yaml")["spec"]["endpoints"]
        rules = endpoint["relabelings"]
        self.assertTrue(all(r["targetLabel"] == "service" for r in rules))
        self.assertEqual({r.get("replacement") for r in rules if r["regex"] == ""}, {"all"})


if __name__ == "__main__":
    unittest.main()
