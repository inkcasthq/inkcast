"""Architecture rules, checked on every build:

1. Shared libraries never import services (``inkcast.libs`` ↛ ``inkcast.services``).
2. A service never imports another service; services share only libs, contracts, the store and HTTP.
3. Every service is CamelCase, has a unique kebab full name, and its public paths match the route table.
4. The kustomize route manifests agree with the route table.
5. ``libs.secrets`` is the bottom layer: it imports nothing else from ``inkcast`` (logging, config and HTTP
   all depend on it, never the reverse), and every ``reveal()`` lives in a file that is allowed to use one.
6. Every source file carries the licence header.
7. ``Store.erase_user`` covers every table that holds a person's rows (issue #65): the live schema is scanned.
"""

import ast
import pathlib
import unittest

import yaml

from inkcast.launcher import ALL, BUILTIN, LEGACY_MODES, resolve
from inkcast.libs.config import ConfigError
from inkcast.libs.contracts.routes import ROUTES, match
from inkcast.libs.service import app_name_of, full_name

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
ROOT = SRC.parent


def imports(path: pathlib.Path) -> set[str]:
    module = ".".join(path.relative_to(SRC).with_suffix("").parts)
    package = module[: -len(".__init__")] if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found = set()
    tree = ast.parse(path.read_text())
    # `if TYPE_CHECKING:` imports are for type checkers only; they never run, so they are not a dependency.
    typing_only = {id(n) for top in ast.walk(tree) if isinstance(top, ast.If)
                   and isinstance(top.test, ast.Name) and top.test.id == "TYPE_CHECKING" for n in ast.walk(top)}
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.add(target)
            found |= {f"{target}.{a.name}" for a in node.names}
    return found


class Architecture(unittest.TestCase):
    def test_libs_never_import_services(self):
        offenders = {}
        for path in (SRC / "inkcast" / "libs").rglob("*.py"):
            bad = sorted(i for i in imports(path) if i.startswith("inkcast.services"))
            if bad:
                offenders[str(path.relative_to(SRC))] = bad
        self.assertEqual(offenders, {})

    def test_services_never_import_each_other(self):
        offenders = {}
        for service_dir in (SRC / "inkcast" / "services").iterdir():
            if not service_dir.is_dir() or service_dir.name.startswith("_"):
                continue
            own = f"inkcast.services.{service_dir.name}"
            for path in service_dir.rglob("*.py"):
                bad = sorted(i for i in imports(path)
                             if i.startswith("inkcast.services.") and not (i == own or i.startswith(own + ".")))
                if bad:
                    offenders[str(path.relative_to(SRC))] = bad
        self.assertEqual(offenders, {})

    def test_secrets_is_the_bottom_layer(self):
        offenders = {}
        for path in (SRC / "inkcast" / "libs" / "secrets").rglob("*.py"):
            bad = sorted(i for i in imports(path) if i.startswith("inkcast.") and not i.startswith("inkcast.libs.secrets"))
            if bad:
                offenders[str(path.relative_to(SRC))] = bad
        self.assertEqual(offenders, {}, "libs/secrets must not depend on the rest of inkcast (it is imported by all of it)")

    def test_reveal_is_only_called_where_a_credential_is_used(self):
        """Every ``.reveal()`` is a place a secret leaves its wrapper: keep the list short and reviewed."""
        allowed = {
            "inkcast/libs/secrets",                 # the wrapper and the provider themselves
            "inkcast/libs/config.py",               # validate(): lengths only
            "inkcast/services/identity/login.py",   # the HMAC key, the IdP client secrets
            "inkcast/libs/cache/redis.py",          # the AUTH command to Redis/Valkey
            "inkcast/services/identity/keycloak_sync.py",  # the sync service account's client secret, token request
            "inkcast/libs/authz/openfga.py",        # OpenFGA's preshared key, the bearer header of each request
        }
        found = {}
        for path in (SRC / "inkcast").rglob("*.py"):
            rel = str(path.relative_to(SRC))
            if ".reveal(" in path.read_text() and not any(rel.startswith(a) for a in allowed):
                found[rel] = path.read_text().count(".reveal(")
        self.assertEqual(found, {}, "a new .reveal() call site: add it to the allow-list above if it is justified")

    def test_every_source_file_has_the_licence_header(self):
        missing = [str(p.relative_to(ROOT)) for d in ("src", "scripts") for p in (ROOT / d).rglob("*.py")
                   if "static" not in p.parts and "LicenseRef-PolyForm-Shield-1.0.0" not in p.read_text()[:300]]
        self.assertEqual(missing, [])

    def test_service_names(self):
        names = [s.full_name for s in BUILTIN.values()]
        self.assertEqual(len(names), len(set(names)))
        for name, spec in BUILTIN.items():
            self.assertEqual(name, spec.name)
            self.assertEqual(app_name_of(spec.full_name), name)
        self.assertEqual(full_name("PublishingApi"), "publishing-api")
        with self.assertRaises(ValueError):
            full_name("publishing_api")

    def test_public_paths_match_the_route_table(self):
        for name, spec in BUILTIN.items():
            if spec.kind == "http" and name != "Gateway":
                self.assertEqual(tuple(spec.public_paths), ROUTES.get(name, ()), name)
        self.assertEqual(match("/api/v1/tokens/tok_1"), "IdentityService")
        self.assertEqual(match("/api/v1/posts"), "PublishingApi")
        self.assertEqual(match("/.well-known/oauth-protected-resource/mcp"), "PublishingApi")
        self.assertEqual(match("/.well-known/oauth-authorization-server"), "IdentityService")
        self.assertEqual(match("/connect/abc"), "ConnectorService")
        self.assertEqual(match("/tokens/new"), "IdentityService")
        self.assertIsNone(match("/apix"))
        self.assertIsNone(match("/"))

    def test_resolve_app_names(self):
        self.assertEqual([s.name for s in resolve("All")], list(ALL))
        self.assertEqual([s.name for s in resolve("PublishingApi")], ["PublishingApi"])
        self.assertEqual([s.name for s in resolve("ConnectorService, IdentityService")],
                         ["ConnectorService", "IdentityService"])
        self.assertEqual([s.name for s in resolve(LEGACY_MODES["worker"])], ["SchedulerWorker"])
        with self.assertRaisesRegex(ConfigError, "CamelCase"):
            resolve("publishing-api")
        with self.assertRaisesRegex(ConfigError, "Unknown service"):
            resolve("Nope")

    def test_gateway_api_routes_agree_with_the_table(self):
        path = ROOT / "deploy" / "kustomize" / "components" / "gateway-api" / "httproutes.yaml"
        docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
        declared: dict[str, set[str]] = {}
        for route in docs:
            for rule in route["spec"]["rules"]:
                backend = rule["backendRefs"][0]["name"]
                for m in rule["matches"]:
                    declared.setdefault(backend, set()).add(m["path"]["value"])
        expected = {"inkcast-" + full_name(svc): {p.rstrip("/") or "/" for p in paths} for svc, paths in ROUTES.items()}
        self.assertEqual(declared, expected)


    def test_erase_user_covers_every_table_that_holds_a_person(self):
        """Issue #65: a new table with a ``user_id`` column, or one keyed by a user some other way, fails the build
        until ``libs/db/erasure.USER_TABLES`` names it (and the test in test_units seeds it)."""
        from inkcast.libs.db import Store
        from inkcast.libs.db.erasure import USER_TABLES
        # Tables without a user_id column that still hold a person's rows, and the column that holds the key.
        user_keyed = {"kpi_rollups": "scope_id", "authz_tuples": "subject", "users": "id"}
        # Tables with a user_id column that erase_user leaves alone on purpose, each with the reason.
        kept = {"deletion_requests": "the pending 'delete my account' record outlives the erasure so the visit back from "
                                     "Keycloak can be answered; user id and hashes only; expires and is purged, deleted once answered"}
        # Tables that hold no personal data at all, each with the reason (review when adding one).
        impersonal = {
            "oauth_clients": "client registrations, not people",
            "heartbeats": "liveness of background loops",
            "sync_state": "the Keycloak sync's cursors",
            "outbox_cursors": "consumer positions in the outbox",
            "kpi_applied": "event ids already folded into rollups (opaque)",
            "user_erasures": "the audit record: a salted hash, the time and counts",
        }
        store = Store(":memory:", "k" * 40)
        tables = [r["name"] for r in store._all("SELECT name FROM sqlite_master WHERE type = 'table'")
                  if not r["name"].startswith("sqlite_")]
        self.assertGreater(len(tables), 15, "the scan must see the schema, not silently nothing")
        covered = dict(USER_TABLES)
        for table in tables:
            columns = {r["name"] for r in store._all(f"PRAGMA table_info({table})")}
            if table in kept:
                self.assertIn("user_id", columns)
                self.assertNotIn(table, covered, f"{table} is kept through the erasure on purpose: {kept[table]}")
            elif "user_id" in columns:
                self.assertIn(table, covered, f"{table} has a user_id column: add it to erasure.USER_TABLES")
                self.assertIn("user_id = :user_id", covered[table], table)
            elif table in user_keyed:
                self.assertIn(table, covered, f"{table} is keyed by a user ({user_keyed[table]}): add it to USER_TABLES")
                self.assertIn(user_keyed[table], covered[table], table)
            else:
                self.assertIn(table, impersonal, f"{table}: say here why it holds nothing about a person, or erase it")
        self.assertEqual(set(covered) - set(tables), set(), "USER_TABLES names a table the schema doesn't have")
        self.assertEqual(set(impersonal) & set(covered), set())
        self.assertEqual(set(impersonal) - set(tables), set(), "an impersonal table that no longer exists")
        store.close()

    def test_generated_manifests_are_current(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-manifests.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()


class Workflows(unittest.TestCase):
    """CI runs on a pinned runner image (issue #16): `ubuntu-latest` moves to a new Ubuntu on GitHub's date, not
    ours, and kind, Docker Compose and Playwright's system packages all depend on it. Move deliberately, in a PR."""

    def test_the_image_installs_only_hash_locked_dependencies(self):
        """Issue #36: the image's Python packages come from requirements.lock and build.lock with --require-hashes,
        the wheel is built without build isolation (nothing unlocked fetched) and installed with --no-deps; every pin
        in the locks is exact and carries hashes."""
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1]
        docker = (root / "Dockerfile").read_text()
        self.assertIn("--require-hashes --no-deps -r build.lock", docker)
        self.assertIn("--require-hashes --no-deps -r /tmp/requirements.lock", docker)
        self.assertIn("--no-build-isolation", docker)
        self.assertIn("pip install --no-deps /tmp/*.whl", docker)
        self.assertNotRegex(docker, r"pip install (?!--)(?!-r)\S", "a pip install that resolves outside the locks")
        for lock in ("requirements.lock", "build.lock"):
            text = (root / lock).read_text()
            pins = re.findall(r"^([A-Za-z0-9_.-]+)==\S+", text, re.M)
            self.assertTrue(pins, f"{lock} pins nothing")
            entries = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", text.split("\n", 2)[2] if text.startswith("#") else text)
            for entry in entries:
                if re.match(r"[A-Za-z0-9_.-]+==", entry):
                    self.assertIn("--hash=sha256:", entry, f"{lock}: {entry.split()[0]} has no hash")

    def test_kind_runs_the_pinned_node_image(self):
        """Issue #37: every `kind create cluster` uses KIND_NODE_IMAGE from scripts/versions.env (a digest-pinned
        kindest/node), and the kind and kubectl binaries are installed by scripts/kind-tools.sh, checked against the
        SHA-256 pinned there, never taken from whatever the runner image ships."""
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1]
        versions = dict(re.findall(r"^([A-Za-z0-9_]+)=(.*)$", (root / "scripts" / "versions.env").read_text(), re.M))
        self.assertRegex(versions["KIND_NODE_IMAGE"], r"^kindest/node:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")
        self.assertRegex(versions["KIND_SHA256_linux_amd64"], r"^[0-9a-f]{64}$")
        self.assertRegex(versions["KUBECTL_SHA256_linux_amd64"], r"^[0-9a-f]{64}$", "pin kubectl's SHA-256")
        node_minor = int(re.search(r":v1\.(\d+)\.", versions["KIND_NODE_IMAGE"]).group(1))
        kubectl_minor = int(re.search(r"^v1\.(\d+)\.", versions["KUBECTL_VERSION"]).group(1))
        self.assertLessEqual(abs(node_minor - kubectl_minor), 1, "kubectl within one minor of the cluster")
        creates = []
        for path in [*(root / ".github" / "workflows").glob("*.yml"), *(root / "scripts").rglob("*.sh")]:
            for line in path.read_text().splitlines():
                if "kind create cluster" in line and not line.lstrip().startswith("#"):
                    creates.append((path.name, line.strip()))
                    self.assertIn('--image "$KIND_NODE_IMAGE"', line, f"{path.name}: {line.strip()}")
        self.assertGreaterEqual(len(creates), 2, "the check must see CI and stack.sh")
        ci = (root / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("scripts/kind-tools.sh", ci)

    def test_each_image_is_the_same_version_everywhere(self):
        """Dependabot moves the Dockerfile and Compose pins but not the Kubernetes manifests or workflows: one image at
        two versions means Compose and kind test different things. Every reference to an image names the same
        tag and digest; a Dependabot bump is completed by updating the other references (scripts/images.py list)."""
        import importlib.util
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("images", root / "scripts" / "images.py")
        images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(images)
        seen: dict[str, set[str]] = {}
        for _, _, m in images.references():
            seen.setdefault(m["name"], set()).add(f"{m['tag']}@{m['digest']}")
        self.assertEqual({n: v for n, v in seen.items() if len(v) > 1}, {}, "one image, several versions")

    def test_every_third_party_image_is_pinned_by_digest(self):
        """Issue #35: a tag can be re-pushed, a digest can't. scripts/images.py finds every third-party image reference
        (FROM, image:, docker run) and each one must be name:tag@sha256:...; `scripts/images.py pin` adds them."""
        import importlib.util
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("images", root / "scripts" / "images.py")
        images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(images)
        refs = images.references()
        names = {m["name"] for _, _, m in refs}
        self.assertTrue({"python", "quay.io/keycloak/keycloak", "quay.io/prometheus/prometheus"} <= names,
                        f"the scan must see the images, not silently nothing: {sorted(names)}")
        unpinned = sorted(f"{p.relative_to(root)}:{n} {m.group(0)}" for p, n, m in refs if not m["digest"])
        self.assertEqual(unpinned, [], "pin with: scripts/images.py pin (needs docker), or --from a resolve list")

    def test_every_job_runs_on_a_pinned_image(self):
        import pathlib
        import re
        workflows = sorted((pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))
        self.assertTrue(workflows)
        runners = []
        for path in workflows:
            for value in re.findall(r"^\s*runs-on:\s*(\S+)", path.read_text(), re.M):
                runners.append((path.name, value))
                self.assertNotRegex(value, r"-latest$", f"{path.name}: pin the runner image, not {value}")
        self.assertGreater(len(runners), 10, "the check must see the jobs, not silently nothing")

    def test_codeql_runs_as_one_configuration_on_pull_requests_and_main(self):
        """Issue #15: CodeQL inside ci.yml ran as ci.yml:codeql on PRs but publish.yml:codeql on main (ci.yml is
        publish.yml's gate), so GitHub could not compare a PR with main. It lives in its own workflow."""
        import pathlib
        import yaml
        root = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
        holders = [p.name for p in root.glob("*.yml") if "github/codeql-action/init" in p.read_text()]
        self.assertEqual(holders, ["codeql.yml"])
        self.assertIn("config-file: ./.github/codeql/codeql-config.yml", (root / "codeql.yml").read_text())
        triggers = yaml.safe_load((root / "codeql.yml").read_text())[True]   # YAML reads the key `on` as True
        self.assertIn("pull_request", triggers)
        self.assertEqual(triggers["push"]["branches"], ["main"])

    def test_every_required_check_is_a_job_that_runs_on_pull_requests(self):
        """Issue #34: a required check that no job produces blocks every merge forever, so each required context in
        .github/rulesets/main.json must be a job (or matrix entry) of a workflow that runs on pull_request, and
        docs/bootstrap.md must list the same checks."""
        import json
        import pathlib
        import yaml
        root = pathlib.Path(__file__).resolve().parents[1]
        ruleset = json.loads((root / ".github" / "rulesets" / "main.json").read_text())
        rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")
        required = {c["context"] for c in rule["parameters"]["required_status_checks"]}
        produced = {"CodeQL"}                       # the code-scanning results check, from codeql.yml's upload
        for path in (root / ".github" / "workflows").glob("*.yml"):
            flow = yaml.safe_load(path.read_text())
            if "pull_request" not in (flow.get(True) or {}):   # YAML reads the key `on` as True
                continue
            for name, job in flow["jobs"].items():
                matrix = (job.get("strategy") or {}).get("matrix") or {}
                if len(matrix) == 1 and isinstance(next(iter(matrix.values())), list):
                    produced |= {f"{name} ({v})" for v in next(iter(matrix.values()))}
                else:
                    produced.add(job.get("name", name))
        self.assertEqual(required - produced, set(), "required but no pull-request job produces it")
        bootstrap = (root / "docs" / "bootstrap.md").read_text()
        missing = sorted(c for c in required if f"`{c}`" not in bootstrap)
        self.assertEqual(missing, [], "docs/bootstrap.md must list every required check")
        self.assertEqual(ruleset["bypass_actors"], [], "nobody bypasses main's ruleset")

    def test_the_local_codeql_run_is_the_one_ci_runs(self):
        """Issue #28: scripts/dev/codeql.sh pins the CodeQL bundle of the github/codeql-action version codeql.yml
        pins, and both read .github/codeql/codeql-config.yml. Bump the action, bump the script (its comment says how)."""
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "codeql.yml").read_text()
        script = (root / "scripts" / "dev" / "codeql.sh").read_text()
        pinned = set(re.findall(r"github/codeql-action/\w+@([0-9a-f]{40})", workflow))
        self.assertEqual(len(pinned), 1, f"init and analyze pin one action commit: {pinned}")
        self.assertEqual(re.search(r"^CODEQL_ACTION_SHA=([0-9a-f]{40})", script, re.M).group(1), pinned.pop(),
                         "codeql.yml moved to another action version: update CODEQL_BUNDLE and its SHA-256 too")
        self.assertRegex(script, r"(?m)^CODEQL_BUNDLE=codeql-bundle-v\d+\.\d+\.\d+$")
        for platform in ("linux64", "osx64"):
            self.assertRegex(script, rf"(?m)^SHA256_{platform}=[0-9a-f]{{64}}$")
        self.assertIn(".github/codeql/codeql-config.yml", script)
        self.assertIn("security-extended", (root / ".github" / "codeql" / "codeql-config.yml").read_text())

    def test_the_image_runs_a_python_the_tests_run_on(self):
        """Issue #24: the Dockerfile's Python, every stage, is in the CI test matrix; the matrix starts at the
        package's minimum (requires-python), so what we claim to support is what we test."""
        import pathlib
        import re
        import yaml
        root = pathlib.Path(__file__).resolve().parents[1]
        image = set(re.findall(r"^FROM python:(\d+\.\d+)", (root / "Dockerfile").read_text(), re.M))
        self.assertEqual(len(image), 1, f"one Python across the Dockerfile's stages: {image}")
        ci = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text())
        matrix = ci["jobs"]["test"]["strategy"]["matrix"]["python"]
        self.assertLessEqual(image, set(matrix), "the image's Python must be one the tests run on")
        floor = re.search(r'requires-python = ">=(\d+\.\d+)"', (root / "pyproject.toml").read_text()).group(1)
        key = lambda v: tuple(map(int, v.split(".")))
        self.assertEqual(min(matrix, key=key), floor, "test the oldest Python the package claims to support")
