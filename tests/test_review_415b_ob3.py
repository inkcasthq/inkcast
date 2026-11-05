# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3 review of #348 part B: test_web_lock.Lock against locks npm 11.20.0 (Node 26.13.0) really writes.

1. A correct lock with a workspace must pass: npm records each workspace as a link, `"resolved": "packages/foo"`,
   which is not a registry package and carries no integrity.
2. Drifts that `npm ci` lets through must fail. `npm ci` checks one direction only: every package the manifests ask
   for must be in the lock (lib/utils/validate-lockfile.js loops over the ideal tree). A lock that holds MORE than the
   manifests ask for installs and exits 0, so ci.yml's web job passes when a dependency or a workspace is removed
   from a manifest.

The locks below are what that npm wrote (`npm install --package-lock-only`) for the manifests here; each drift was run
through the web job's commands in node:26.13.0-bookworm-slim and passed `npm ci` and the `git diff` check."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

import test_web_lock

# web/package.json plus packages/foo depending on left-pad 1.3.0 and is-number 7.0.0.
LOCK_WORKSPACE = {
    "name": "inkcast-web",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {
        "": {
            "name": "inkcast-web",
            "license": "LicenseRef-PolyForm-Shield-1.0.0",
            "workspaces": [
                "apps/*",
                "packages/*"
            ],
            "devDependencies": {
                "typescript": "6.0.3"
            },
            "engines": {
                "node": "26.13.0"
            }
        },
        "node_modules/foo": {
            "resolved": "packages/foo",
            "link": True
        },
        "node_modules/is-number": {
            "version": "7.0.0",
            "resolved": "https://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz",
            "integrity": "sha512-41Cifkg6e8TylSpdtTpeLVMqvSBEVzTttHvERD741+pnZ8ANv0004MRL43QKPDlK9cGvNp6NZWZUBlbGXYxxng==",
            "license": "MIT",
            "engines": {
                "node": ">=0.12.0"
            }
        },
        "node_modules/left-pad": {
            "version": "1.3.0",
            "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
            "integrity": "sha512-XI5MPzVNApjAyhQzphX8BkmKsKUxD4LdyK24iZeQGinBN9yTQT3bFlCBy/aVx2HrNcqQGsdot8ghrjyrvMCoEA==",
            "deprecated": "use String.prototype.padStart()",
            "license": "WTFPL"
        },
        "node_modules/typescript": {
            "version": "6.0.3",
            "resolved": "https://registry.npmjs.org/typescript/-/typescript-6.0.3.tgz",
            "integrity": "sha512-y2TvuxSZPDyQakkFRPZHKFm+KKVqIisdg9/CZwm9ftvKXLP8NRWj38/ODjNbr43SsoXqNuAisEf1GdCxqWcdBw==",
            "dev": True,
            "license": "Apache-2.0",
            "bin": {
                "tsc": "bin/tsc",
                "tsserver": "bin/tsserver"
            },
            "engines": {
                "node": ">=14.17"
            }
        },
        "packages/foo": {
            "version": "0.0.0",
            "dependencies": {
                "is-number": "7.0.0",
                "left-pad": "1.3.0"
            }
        }
    }
}

# web/package.json plus root dependencies left-pad 1.3.0 and is-number 7.0.0.
LOCK_ROOT_DEPENDENCIES = {
    "name": "inkcast-web",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {
        "": {
            "name": "inkcast-web",
            "license": "LicenseRef-PolyForm-Shield-1.0.0",
            "workspaces": [
                "apps/*",
                "packages/*"
            ],
            "dependencies": {
                "is-number": "7.0.0",
                "left-pad": "1.3.0"
            },
            "devDependencies": {
                "typescript": "6.0.3"
            },
            "engines": {
                "node": "26.13.0"
            }
        },
        "node_modules/is-number": {
            "version": "7.0.0",
            "resolved": "https://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz",
            "integrity": "sha512-41Cifkg6e8TylSpdtTpeLVMqvSBEVzTttHvERD741+pnZ8ANv0004MRL43QKPDlK9cGvNp6NZWZUBlbGXYxxng==",
            "license": "MIT",
            "engines": {
                "node": ">=0.12.0"
            }
        },
        "node_modules/left-pad": {
            "version": "1.3.0",
            "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
            "integrity": "sha512-XI5MPzVNApjAyhQzphX8BkmKsKUxD4LdyK24iZeQGinBN9yTQT3bFlCBy/aVx2HrNcqQGsdot8ghrjyrvMCoEA==",
            "deprecated": "use String.prototype.padStart()",
            "license": "WTFPL"
        },
        "node_modules/typescript": {
            "version": "6.0.3",
            "resolved": "https://registry.npmjs.org/typescript/-/typescript-6.0.3.tgz",
            "integrity": "sha512-y2TvuxSZPDyQakkFRPZHKFm+KKVqIisdg9/CZwm9ftvKXLP8NRWj38/ODjNbr43SsoXqNuAisEf1GdCxqWcdBw==",
            "dev": True,
            "license": "Apache-2.0",
            "bin": {
                "tsc": "bin/tsc",
                "tsserver": "bin/tsserver"
            },
            "engines": {
                "node": ">=14.17"
            }
        }
    }
}

FOO = {"name": "foo", "version": "0.0.0", "private": True, "dependencies": {"is-number": "7.0.0", "left-pad": "1.3.0"}}


def lock_tests(manifest, lock, workspaces):
    """Run test_web_lock.Lock against a web/ tree made of these files; return the unittest result."""
    with tempfile.TemporaryDirectory() as tmp:
        web = pathlib.Path(tmp) / "web"
        for rel, body in {"package.json": manifest, "package-lock.json": lock, **workspaces}.items():
            (web / rel).parent.mkdir(parents=True, exist_ok=True)
            (web / rel).write_text(json.dumps(body, indent=2) + "\n")
        with mock.patch.object(test_web_lock, "ROOT", web.parent), \
                mock.patch.object(test_web_lock, "MANIFEST", web / "package.json"):
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(test_web_lock.Lock).run(result)
    return result


class LockDrift(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(test_web_lock.MANIFEST.read_text())
        self.with_dependencies = {**self.manifest, "dependencies": {"is-number": "7.0.0", "left-pad": "1.3.0"}}

    def test_locks_npm_wrote_for_these_manifests_pass(self):
        for manifest, lock, workspaces in (
            (self.manifest, LOCK_WORKSPACE, {"packages/foo/package.json": FOO}),
            (self.with_dependencies, LOCK_ROOT_DEPENDENCIES, {}),
        ):
            result = lock_tests(manifest, lock, workspaces)
            self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_a_dependency_removed_from_a_workspace_fails(self):
        foo = {**FOO, "dependencies": {"left-pad": "1.3.0"}}
        self.assertFalse(lock_tests(self.manifest, LOCK_WORKSPACE, {"packages/foo/package.json": foo}).wasSuccessful())

    def test_a_workspace_removed_fails(self):
        self.assertFalse(lock_tests(self.manifest, LOCK_WORKSPACE, {}).wasSuccessful())

    def test_a_dependency_removed_from_web_package_json_dependencies_fails(self):
        manifest = {**self.manifest, "dependencies": {"left-pad": "1.3.0"}}
        self.assertFalse(lock_tests(manifest, LOCK_ROOT_DEPENDENCIES, {}).wasSuccessful())


if __name__ == "__main__":
    unittest.main()
