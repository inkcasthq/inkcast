# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #547 (Codex): scripts/web-lock.sh replaces the lock only once a whole resolution and its diff succeeded,
so no failure or signal leaves it changed or partial; its node_modules guard sees links too; and the existing tests
fail when what they name breaks."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import test_web_lock as existing


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
OLD = {"lockfileVersion": 3, "packages": {"": {"name": "old"}}}
NEW = {"lockfileVersion": 3, "packages": {"": {"name": "new"}}}


@unittest.skipUnless(NODE, "Node is needed to test the diff printer")
class LockScriptRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("scripts", "web", "bin"):
            (self.root / directory).mkdir()
        shutil.copy(ROOT / "scripts/web-lock.sh", self.root / "scripts/web-lock.sh")
        shutil.copy(ROOT / "web/package.json", self.root / "web/package.json")
        self.lock = self.root / "web/package-lock.json"
        self.lock.write_text(json.dumps(OLD) + "\n")
        self.pinned = json.loads((ROOT / "web/package.json").read_text())["engines"]["node"]
        self.shim("node", f'''#!/bin/bash
if [ "$1" = --version ]; then echo v{self.pinned}; exit 0; fi
exec "{NODE}" "$@"
''')
        self.shim("npm", '''#!/bin/bash
if [ "$1" = --version ]; then echo 11.0.0; exit 0; fi
cp "$REVIEW_NEW_LOCK" package-lock.json
''')
        self.new_lock = self.root / "new-lock.json"
        self.new_lock.write_text(json.dumps(NEW) + "\n")
        self.env = {**os.environ, "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
                    "REVIEW_NEW_LOCK": str(self.new_lock)}

    def shim(self, name, body):
        path = self.root / "bin" / name
        path.write_text(body)
        path.chmod(stat.S_IRWXU)

    def run_script(self):
        return subprocess.run(["bash", str(self.root / "scripts/web-lock.sh"), str(self.root / "moved.txt")],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_restores_old_lock_when_diff_node_fails(self):
        self.shim("node", f'''#!/bin/bash
if [ "$1" = --version ]; then echo v{self.pinned}; exit 0; fi
if [ "$1" = - ]; then exit 7; fi
exec "{NODE}" "$@"
''')
        run = self.run_script()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(json.loads(self.lock.read_text()), OLD, run.stderr)

    def test_removes_new_lock_when_diff_fails_without_old_lock(self):
        self.lock.unlink()
        self.shim("node", f'''#!/bin/bash
if [ "$1" = --version ]; then echo v{self.pinned}; exit 0; fi
if [ "$1" = - ]; then exit 7; fi
exec "{NODE}" "$@"
''')
        run = self.run_script()
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(self.lock.exists(), run.stderr)

    def test_restores_old_lock_when_npm_leaves_a_partial_file(self):
        self.shim("npm", '''#!/bin/bash
if [ "$1" = --version ]; then echo 11.0.0; exit 0; fi
printf '{' > package-lock.json
exit 1
''')
        run = self.run_script()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(json.loads(self.lock.read_text()), OLD, run.stderr)

    def test_resolver_sees_no_old_lock(self):
        self.shim("npm", '''#!/bin/bash
if [ "$1" = --version ]; then echo 11.0.0; exit 0; fi
if [ -e package-lock.json ]; then echo "old lock reached npm" >&2; exit 7; fi
cp "$REVIEW_NEW_LOCK" package-lock.json
''')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(self.lock.read_text()), NEW)

    def test_npm_receives_lock_only_and_ignore_scripts_flags(self):
        args = self.root / "npm-args.txt"
        self.env["REVIEW_NPM_ARGS"] = str(args)
        self.shim("npm", '''#!/bin/bash
if [ "$1" = --version ]; then echo 11.0.0; exit 0; fi
printf '%s\\n' "$@" > "$REVIEW_NPM_ARGS"
cp "$REVIEW_NEW_LOCK" package-lock.json
''')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(args.read_text().splitlines()[:3],
                         ["install", "--package-lock-only", "--ignore-scripts"])

    def test_restores_old_lock_on_termination(self):
        self.shim("npm", '''#!/bin/bash
if [ "$1" = --version ]; then echo 11.0.0; exit 0; fi
printf '{' > package-lock.json
kill -TERM "$PPID"
sleep 0.1
''')
        run = self.run_script()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(json.loads(self.lock.read_text()), OLD, run.stderr)

    def test_kill_cannot_leave_a_partial_lock(self):
        self.shim("npm", '''#!/bin/bash
if [ "$1" = --version ]; then echo 11.0.0; exit 0; fi
printf '{' > package-lock.json
kill -KILL "$PPID"
sleep 0.1
''')
        run = self.run_script()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(json.loads(self.lock.read_text()), OLD, run.stderr)

    def test_rejects_node_modules_at_each_workspace_level(self):
        for relative in ("node_modules", "apps/app/node_modules", "packages/ui/node_modules"):
            with self.subTest(relative=relative):
                path = self.root / "web" / relative
                path.mkdir(parents=True)
                run = self.run_script()
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("node_modules found", run.stderr)
                self.assertEqual(json.loads(self.lock.read_text()), OLD)
                shutil.rmtree(path)

    def test_rejects_a_symlinked_node_modules_inside_web(self):
        target = self.root / "shared-modules"
        target.mkdir()
        for relative in ("node_modules", "apps/app/node_modules"):
            with self.subTest(relative=relative):
                path = self.root / "web" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(target, target_is_directory=True)
                run = self.run_script()
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("node_modules found", run.stderr)
                self.assertEqual(json.loads(self.lock.read_text()), OLD)
                path.unlink()

    def test_ignores_node_modules_outside_web(self):
        (self.root / "node_modules").mkdir()
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(self.lock.read_text()), NEW)

    def test_prints_nested_workspace_and_scoped_paths(self):
        before = {"lockfileVersion": 3, "packages": {
            "": {}, "apps/app/node_modules/@scope/name": {"version": "1.0.0"},
            "node_modules/@scope/root": {"version": "1.0.0"}}}
        after = {"lockfileVersion": 3, "packages": {
            "": {}, "apps/app/node_modules/@scope/name": {"version": "2.0.0"},
            "node_modules/@scope/root": {"version": "2.0.0"}}}
        self.lock.write_text(json.dumps(before))
        self.new_lock.write_text(json.dumps(after))
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("apps/app/node_modules/@scope/name 1.0.0 -> 2.0.0", run.stdout)
        self.assertIn("@scope/root 1.0.0 -> 2.0.0", run.stdout)


class ExistingTestSensitivity(unittest.TestCase):
    def test_node_pin_test_rejects_a_higher_priority_volta_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "package.json"
            content = json.loads(existing.MANIFEST.read_text())
            content["volta"] = {"node": "20.0.0"}
            manifest.write_text(json.dumps(content))
            with patch.object(existing, "MANIFEST", manifest):
                case = existing.NodePin("test_node_comes_from_web_package_json_only")
                with self.assertRaises(AssertionError):
                    case.test_node_comes_from_web_package_json_only()

    def test_build_workflow_test_rejects_another_lock_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflows = Path(tmp)
            (workflows / "ci.yml").write_text('jobs:\n  web:\n    steps:\n      - run: scripts/web-lock.sh\n')
            with patch.object(existing, "WORKFLOWS", workflows):
                case = existing.NodePin("test_no_workflow_writes_a_lock")
                with self.assertRaises(AssertionError):
                    case.test_no_workflow_writes_a_lock()


class WorkflowAndDocs(unittest.TestCase):
    def test_unchanged_lock_exits_before_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            git_log = root / "git.log"
            git_shim = bin_dir / "git"
            git_shim.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$REVIEW_GIT_LOG"
if [ "$1" = add ]; then exit 0; fi
if [ "$1" = diff ] && [ "$2" = --cached ] && [ "$3" = --quiet ]; then exit 0; fi
exit 7
''')
            git_shim.chmod(stat.S_IRWXU)
            flow = yaml.safe_load(existing.FLOW.read_text())
            push = next(s for s in flow["jobs"]["refresh"]["steps"] if s.get("name", "").startswith("Push"))
            summary = root / "summary.txt"
            env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": tmp,
                   "REVIEW_GIT_LOG": str(git_log), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            run = subprocess.run(["bash", "-c", push["run"]], cwd=root, env=env,
                                 capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("nothing pushed", summary.read_text())
            self.assertEqual(git_log.read_text().splitlines(),
                             ["add web/package-lock.json", "diff --cached --quiet"])

    def test_run_blocks_have_no_expression_interpolation(self):
        flow = yaml.safe_load(existing.FLOW.read_text())
        for step in flow["jobs"]["refresh"]["steps"]:
            self.assertNotIn("${{", step.get("run", ""))


if __name__ == "__main__":
    unittest.main()
