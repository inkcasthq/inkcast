# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #547 (F4.1.S5): a failed npm resolution must leave the original lock in place."""

import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK = '{"lockfileVersion": 3, "packages": {"": {"name": "inkcast-web"}}}\n'


@unittest.skipUnless(shutil.which("node"), "needs node: the script reads package.json with it")
class AFailedResolution(unittest.TestCase):
    def test_keeps_the_old_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "scripts").mkdir()
            (tmp / "web").mkdir()
            (tmp / "bin").mkdir()
            shutil.copy(ROOT / "scripts" / "web-lock.sh", tmp / "scripts")
            shutil.copy(ROOT / "web" / "package.json", tmp / "web")
            (tmp / "web" / "package-lock.json").write_text(LOCK)
            pinned = json.loads((ROOT / "web" / "package.json").read_text())["engines"]["node"]
            shims = {   # the pinned Node, whatever this machine runs, and an npm that fails as a registry error does
                "node": f'#!/bin/bash\n[ "$1" = --version ] && {{ echo v{pinned}; exit 0; }}\nexec {shutil.which("node")} "$@"\n',
                "npm": '#!/bin/bash\necho "npm error network request failed" >&2\nexit 1\n',
            }
            for name, body in shims.items():
                (tmp / "bin" / name).write_text(body)
                (tmp / "bin" / name).chmod(stat.S_IRWXU)
            env = {**os.environ, "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}"}
            run = subprocess.run(["bash", str(tmp / "scripts" / "web-lock.sh")], env=env, capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0, run.stdout)
            self.assertTrue((tmp / "web" / "package-lock.json").exists(), run.stderr)
            self.assertEqual((tmp / "web" / "package-lock.json").read_text(), LOCK)


if __name__ == "__main__":
    unittest.main()
