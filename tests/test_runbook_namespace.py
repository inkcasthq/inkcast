# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The runbooks name the app's namespace as ``$NS`` instead of hard-coding it (#312).

The overlays deploy to ``inkcast``. The environments deploy to ``inkcast-staging`` and ``inkcast-prod``. A command
that hard-codes one of them finds nothing on the other two, or acts in the wrong namespace. So every runbook sets
``NS`` once, near the top, listing the three values, and every command takes the namespace from it."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNBOOKS = sorted((ROOT / "docs/runbooks").glob("*.md"))
KUSTOMIZATIONS = {
    "overlays/openshift": "inkcast",
    "overlays/kubernetes": "inkcast",
    "environments/staging": "inkcast-staging",
    "environments/production": "inkcast-prod",
}


def deployed_namespace(folder: str) -> str:
    """The ``namespace:`` of ``deploy/kustomize/<folder>/kustomization.yaml``."""
    text = (ROOT / "deploy/kustomize" / folder / "kustomization.yaml").read_text(encoding="utf-8")
    return re.search(r"^namespace:\s*(\S+)\s*$", text, re.MULTILINE).group(1)


def hard_coded(text: str, namespaces: set[str]) -> list[str]:
    """Every command in ``text`` that passes one of ``namespaces`` literally, with ``-n``, ``--namespace`` or as
    the argument of ``scripts/ops/restore.sh`` (which takes the namespace first)."""
    names = "|".join(sorted(namespaces, key=len, reverse=True))
    pattern = re.compile(rf"(?:(?:-n|--namespace)[ =]|restore\.sh )(?:{names})(?![\w-])")
    return [f"{number}: {line.strip()}" for number, line in enumerate(text.splitlines(), 1) if pattern.search(line)]


class RunbooksNameTheNamespace(unittest.TestCase):
    def setUp(self):
        self.assertTrue(RUNBOOKS, "no runbooks under docs/runbooks")
        self.namespaces = {deployed_namespace(folder) for folder in KUSTOMIZATIONS}

    def test_the_kustomizations_still_deploy_where_the_runbooks_say(self):
        for folder, namespace in KUSTOMIZATIONS.items():
            self.assertEqual(deployed_namespace(folder), namespace, folder)

    def test_no_command_hard_codes_the_app_namespace(self):
        for runbook in RUNBOOKS:
            with self.subTest(runbook=runbook.name):
                found = hard_coded(runbook.read_text(encoding="utf-8"), self.namespaces)
                self.assertEqual(found, [], f"{runbook.name} hard-codes the app namespace; use -n \"$NS\"")

    def test_each_runbook_says_where_the_namespace_comes_from_once(self):
        for runbook in RUNBOOKS:
            with self.subTest(runbook=runbook.name):
                text = runbook.read_text(encoding="utf-8")
                for namespace in self.namespaces:
                    self.assertEqual(text.count(f"`NS={namespace}`"), 1, f"{runbook.name}: NS={namespace}")
                self.assertIn('-n "$NS"', text, runbook.name)


if __name__ == "__main__":
    unittest.main()
