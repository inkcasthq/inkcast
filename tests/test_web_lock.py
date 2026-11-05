# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The web workspace's npm lock is resolved in CI (E4.F4.1.S5, issue #348): web-lock-refresh.yml resolves
web/package-lock.json from scratch on Linux and pushes it to a deps/ branch, as lock-refresh.yml does for Python.
What a file can show is checked here; the resolution itself runs in the workflow."""

import json
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FLOW = WORKFLOWS / "web-lock-refresh.yml"
SCRIPT = ROOT / "scripts" / "web-lock.sh"
MANIFEST = ROOT / "web" / "package.json"
EXACT = r"^\d+\.\d+\.\d+$"


class WebLockRefresh(unittest.TestCase):
    def setUp(self):
        self.text = FLOW.read_text()
        self.flow = yaml.safe_load(self.text)

    def test_runs_weekly_and_on_demand_only(self):
        triggers = self.flow[True]   # YAML reads the key `on` as True
        self.assertEqual(set(triggers), {"workflow_dispatch", "schedule"})
        self.assertEqual(len(triggers["schedule"]), 1)
        self.assertRegex(triggers["schedule"][0]["cron"], r"^\d+ \d+ \* \* \d$", "weekly")

    def test_reads_by_default_and_writes_only_where_it_pushes_a_deps_branch(self):
        self.assertEqual(self.flow["permissions"], {"contents": "read"})
        writers = {name: job.get("permissions") for name, job in self.flow["jobs"].items() if job.get("permissions")}
        self.assertEqual(writers, {"refresh": {"contents": "write"}})
        pushes = re.findall(r"git push\b.*", self.text)
        self.assertEqual(pushes, ["git push --force origin HEAD:refs/heads/deps/web-lock-refresh"])
        self.assertNotRegex(self.text, r"(?m)^\s*gh pr create", "a PR opened by the workflow's token runs no CI")

    def test_every_action_is_pinned_by_commit_with_its_version(self):
        uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)(.*)$", self.text, re.M)
        self.assertGreaterEqual(len(uses), 2, "the check must see checkout and setup-node")
        for ref, comment in uses:
            self.assertRegex(ref, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", f"pin {ref} by commit SHA")
            self.assertRegex(comment, r"^\s*# v\d+\.\d+\.\d+$", f"{ref}: name the version in a comment")

    def test_resolves_from_scratch_without_install_scripts(self):
        """The resolution itself (flags, the node_modules guard, recovery) runs in test_review_547_codex.py."""
        steps = self.flow["jobs"]["refresh"]["steps"]
        self.assertIn('scripts/ci-run.sh "web lock" scripts/web-lock.sh "$RUNNER_TEMP/moved.txt"',
                      [s.get("run", "") for s in steps])
        setup = next(s for s in steps if s.get("uses", "").startswith("actions/setup-node@"))
        self.assertEqual(str(setup["with"]["package-manager-cache"]).lower(), "false")

    def test_new_files_carry_the_licence_header(self):
        for path in (FLOW, SCRIPT, pathlib.Path(__file__)):
            self.assertIn("SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0", path.read_text()[:300], path)


class NodePin(unittest.TestCase):
    def test_node_comes_from_web_package_json_only(self):
        """engines.node is the one Node pin: every setup-node reads it with node-version-file, none names a version,
        and no other file pins Node."""
        manifest = json.loads(MANIFEST.read_text())
        self.assertRegex(manifest["engines"]["node"], EXACT, "pin an exact Node version")
        self.assertNotIn("node", manifest.get("volta", {}), "setup-node reads volta.node before engines.node")
        self.assertNotIn("runtime", manifest.get("devEngines", {}), "setup-node reads devEngines.runtime before engines.node")
        setups = 0
        for path in WORKFLOWS.glob("*.yml"):
            for job in yaml.safe_load(path.read_text())["jobs"].values():
                for step in job.get("steps", []):
                    if step.get("uses", "").startswith("actions/setup-node@"):
                        setups += 1
                        self.assertEqual(step["with"].get("node-version-file"), "web/package.json", path.name)
                        self.assertNotIn("node-version", step["with"], path.name)
        self.assertGreaterEqual(setups, 1, "the check must see web-lock-refresh's setup-node")
        self.assertNotRegex((ROOT / "scripts" / "versions.env").read_text(), r"(?m)^NODE", "one place: engines.node")

    def test_no_workflow_writes_a_lock(self):
        """Builds install with `npm ci`, which never writes the lock; only scripts/web-lock.sh resolves one."""
        for path in WORKFLOWS.glob("*.yml"):
            self.assertNotRegex(path.read_text(), r"\bnpm (install|i|update|up)\b", path.name)
            if path.name != "web-lock-refresh.yml":
                self.assertNotIn("scripts/web-lock.sh", path.read_text(), path.name)


class Manifest(unittest.TestCase):
    def test_is_a_private_workspaces_root_with_exact_versions(self):
        manifest = json.loads(MANIFEST.read_text())
        self.assertIs(manifest["private"], True)
        self.assertEqual(manifest["workspaces"], ["apps/*", "packages/*"])
        self.assertIn("typescript", manifest["devDependencies"])
        for field in ("dependencies", "devDependencies", "optionalDependencies"):
            for name, version in manifest.get(field, {}).items():
                self.assertRegex(version, EXACT, f"{field}: pin {name} exactly")


class Lock(unittest.TestCase):
    """web/package-lock.json comes from web-lock-refresh.yml (PR B of #348), and CI installs it with npm ci only."""

    def setUp(self):
        self.manifest = json.loads(MANIFEST.read_text())
        self.lock = json.loads((ROOT / "web" / "package-lock.json").read_text())

    def test_holds_every_pinned_version_of_the_manifest(self):
        self.assertEqual(self.lock["lockfileVersion"], 3)
        self.assertEqual(self.lock["packages"][""]["devDependencies"], self.manifest["devDependencies"])
        for name, version in self.manifest["devDependencies"].items():
            self.assertEqual(self.lock["packages"][f"node_modules/{name}"]["version"], version, name)

    def test_records_the_dependencies_of_the_root_and_every_workspace(self):
        """npm ci fails when the lock lacks what a manifest asks for, not when it holds more (validate-lockfile.js
        checks one direction), so a dependency or a workspace removed from a manifest still passes the web job."""
        web = MANIFEST.parent
        manifests = {"": self.manifest}
        for pattern in self.manifest.get("workspaces", []):
            for path in sorted(web.glob(f"{pattern}/package.json")):
                manifests[path.parent.relative_to(web).as_posix()] = json.loads(path.read_text())
        recorded = {key for key in self.lock["packages"] if "node_modules/" not in key}
        self.assertEqual(recorded, set(manifests), "the lock's workspaces are the ones under web/")
        for key, manifest in manifests.items():
            for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                self.assertEqual(self.lock["packages"][key].get(field, {}), manifest.get(field, {}), f"{key}: {field}")

    def test_every_registry_package_carries_a_sha512_integrity(self):
        # Every installed package is fetched but a workspace, a link ("resolved": "packages/ui", "link": true), and a
        # bundled one. Select by key, not by "resolved": npm ci installs an entry with neither resolved nor integrity.
        fetched = {key: entry for key, entry in self.lock["packages"].items()
                   if "node_modules/" in key and not entry.get("link") and not entry.get("inBundle")}
        self.assertTrue(fetched, "the check must see the registry packages")
        for key, entry in fetched.items():
            self.assertTrue(entry.get("resolved", "").startswith("https://registry.npmjs.org/"), key)
            self.assertRegex(entry.get("integrity", ""), r"^sha512-", key)

    def test_ci_installs_it_with_npm_ci_and_checks_it_is_unchanged(self):
        ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
        self.assertIn("pull_request", ci[True])   # YAML reads the key `on` as True
        job = ci["jobs"]["web"]
        steps = job["steps"]
        install = next(s for s in steps if "npm ci" in s.get("run", ""))
        self.assertEqual(install.get("working-directory"), "web")
        self.assertIn("--ignore-scripts", install["run"])
        self.assertIn("git diff --exit-code -- web/package-lock.json", [s.get("run", "").split('" ', 1)[-1] for s in steps])
        diff = next(s for s in steps if "git diff" in s.get("run", ""))
        self.assertNotIn("working-directory", diff, "the pathspec web/package-lock.json is relative to the repository")
        self.assertLess(steps.index(install), steps.index(diff))
        for scope in (job, install, diff):
            self.assertFalse({"if", "continue-on-error"} & set(scope), "both steps run, and either one fails the job")


if __name__ == "__main__":
    unittest.main()
