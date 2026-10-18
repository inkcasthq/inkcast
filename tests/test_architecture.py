"""Architecture rules, checked on every build:

1. Shared libraries never import services (``inkcast.libs`` ↛ ``inkcast.services``).
2. A service never imports another service; services share only libs, contracts, the store and HTTP.
3. Every service is CamelCase, has a unique kebab full name, and its public paths match the route table.
4. The kustomize route manifests agree with the route table.
5. ``libs.secrets`` is the bottom layer: it imports nothing else from ``inkcast`` (logging, config and HTTP
   all depend on it, never the reverse), and every ``reveal()`` lives in a file that is allowed to use one.
6. Every source file carries the licence header.
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


    def test_generated_manifests_are_current(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-manifests.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
