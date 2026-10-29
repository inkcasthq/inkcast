# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The runbooks name the app's namespace as ``$NS`` instead of hard-coding it (#312).

The overlays deploy to ``inkcast``. The environments deploy to ``inkcast-staging`` and ``inkcast-prod``. A command
that hard-codes one of them finds nothing on the other two, or acts in the wrong namespace. So no runbook command
passes one of them literally, and a runbook that uses ``$NS`` sets it once, near the top, listing the three values.
A runbook that never touches the app's namespace (one about Keycloak, say) needs no such paragraph."""

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
USES_NS = re.compile(r'"\$NS"|\$\{NS\}')


def deployed_namespace(folder: str) -> str:
    """The ``namespace:`` of ``deploy/kustomize/<folder>/kustomization.yaml``."""
    text = (ROOT / "deploy/kustomize" / folder / "kustomization.yaml").read_text(encoding="utf-8")
    return re.search(r"^namespace:\s*(\S+)\s*$", text, re.MULTILINE).group(1)


def detector(namespaces: set[str]) -> re.Pattern[str]:
    """Matches a command that passes one of ``namespaces`` literally: after ``-n`` or ``--namespace`` (joined, or
    separated by blanks or ``=``), after ``oc project``, or as the first argument of ``scripts/ops/restore.sh``.
    Quotes around the name are allowed. ``inkcast-db`` and ``inkcast-20261018T031700Z.db`` are not hits: the name
    must end there. One regex, not a shell lexer, so ``echo -n inkcast`` in prose is an accepted false positive."""
    names = "|".join(re.escape(name) for name in sorted(namespaces, key=len, reverse=True))
    return re.compile(
        r"(?<![\w-])(?:(?:-n|--namespace)(?:[ \t]+|=)?|oc[ \t]+project[ \t]+|restore\.sh[ \t]+)"
        rf"[\"']?(?:{names})[\"']?(?![\w-])"
    )


def hard_coded(text: str, namespaces: set[str]) -> list[str]:
    """Every line of ``text`` that passes one of ``namespaces`` literally, as ``<number>: <line>``."""
    pattern = detector(namespaces)
    return [f"{number}: {line.strip()}" for number, line in enumerate(text.splitlines(), 1) if pattern.search(line)]


def setup_missing(text: str, namespaces: set[str]) -> list[str]:
    """What a runbook ``text`` that uses ``$NS`` fails to say: each ``NS=<namespace>`` it does not name in backticks
    exactly once. A text that never uses ``$NS`` has nothing to say, so the list is empty."""
    if not USES_NS.search(text):
        return []
    return [f"NS={name}" for name in sorted(namespaces) if text.count(f"`NS={name}`") != 1]


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

    def test_a_runbook_that_uses_ns_says_where_it_comes_from_once(self):
        for runbook in RUNBOOKS:
            with self.subTest(runbook=runbook.name):
                missing = setup_missing(runbook.read_text(encoding="utf-8"), self.namespaces)
                self.assertEqual(missing, [], f"{runbook.name} uses $NS but does not set it once")

    def test_the_runbooks_that_act_on_the_app_use_ns(self):
        """So the rule above cannot pass because no runbook uses ``$NS`` at all."""
        for name in ("alerts.md", "backup-restore.md"):
            with self.subTest(runbook=name):
                self.assertRegex((ROOT / "docs/runbooks" / name).read_text(encoding="utf-8"), USES_NS)


class TheDetector(unittest.TestCase):
    NAMESPACES = {"inkcast", "inkcast-staging", "inkcast-prod"}
    FOUND = (
        "oc -n {ns} get pods",
        "oc -n{ns} get pods",
        "oc -n '{ns}' get pods",
        'oc -n "{ns}" get pods',
        "oc -n  {ns} get pods",
        "oc -n\t{ns} get pods",
        "kubectl --namespace {ns} get pods",
        "kubectl --namespace={ns} get pods",
        "oc project {ns}",
        "KUBECTL=oc scripts/ops/restore.sh {ns} --list",
        'KUBECTL=oc scripts/ops/restore.sh "{ns}"',
        "`oc -n {ns} logs deploy/inkcast | grep identity.erasure.reapplied`.",
    )
    NOT_FOUND = (
        'oc -n "$NS" get pods',
        'kubectl --namespace="${NS}" get pods',
        "oc -n keycloak get pods",
        "oc -n inkcast-other get pods",
        "oc -n inkcast-db get pods",
        'scripts/ops/restore.sh "$NS" inkcast-20261018T031700Z.db',
        "oc get servicemonitor inkcast",
        "oc -n openshift-user-workload-monitoring get pods",
        "oc -n inkcast-backup-now get pods",
    )

    def test_every_form_of_a_hard_coded_namespace_is_found(self):
        for form in self.FOUND:
            for namespace in sorted(self.NAMESPACES):
                line = form.format(ns=namespace)
                with self.subTest(line=line):
                    self.assertEqual(hard_coded(line, self.NAMESPACES), [f"1: {line}"])

    def test_the_variable_and_other_namespaces_are_not_found(self):
        for line in self.NOT_FOUND:
            with self.subTest(line=line):
                self.assertEqual(hard_coded(line, self.NAMESPACES), [])

    def test_a_runbook_without_the_app_namespace_needs_no_setup(self):
        text = "# Keycloak\n\n`oc -n keycloak get keycloakrealmimports`\n"
        self.assertEqual(hard_coded(text, self.NAMESPACES), [])
        self.assertEqual(setup_missing(text, self.NAMESPACES), [])

    def test_a_runbook_that_uses_ns_must_set_all_three(self):
        text = 'Set `NS=inkcast` first.\n\n`oc -n "$NS" get pods`\n'
        self.assertEqual(setup_missing(text, self.NAMESPACES), ["NS=inkcast-prod", "NS=inkcast-staging"])
        self.assertEqual(setup_missing(text.replace('"$NS"', '"${NS}"'), self.NAMESPACES),
                         ["NS=inkcast-prod", "NS=inkcast-staging"])


if __name__ == "__main__":
    unittest.main()
