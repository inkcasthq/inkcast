"""The API-docs bundles are present, unmodified, wired in, and never fetched from a CDN.

/api (Swagger UI) and /api/redoc render from files committed under
src/inkcast/static/vendor, pinned in ASSETS.lock by scripts/vendor-assets.sh (which
verifies npm's published integrity before recording a hash). A build that silently
fell back to a CDN would render blank pages on an air-gapped cluster and widen the
supply chain, so each failure mode has a test.
"""

import hashlib
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDOR = ROOT / "src" / "inkcast" / "static" / "vendor"
LOCK = VENDOR / "ASSETS.lock"
EXPECTED = {"redoc.standalone.js", "swagger-ui-bundle.js", "swagger-ui.css"}


def locked() -> dict[str, str]:
    out = {}
    for line in LOCK.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            digest, name = line.split(None, 1)
            out[name] = digest
    return out


class VendoredAssets(unittest.TestCase):
    def test_bundles_are_committed_and_locked(self):
        present = {p.name for p in VENDOR.iterdir() if p.suffix in {".js", ".css"}}
        self.assertTrue(EXPECTED <= present, EXPECTED - present)
        self.assertEqual(set(locked()), EXPECTED)

    def test_each_bundle_matches_the_lock(self):
        for name, digest in locked().items():
            actual = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, f"{name} was modified; re-pin with scripts/vendor-assets.sh --update")

    def test_versions_are_pinned(self):
        versions = dict(re.findall(r"^# version (\S+) (\S+)$", LOCK.read_text(), re.M))
        self.assertEqual(set(versions), {"redoc", "swagger-ui-dist"})

    def test_offline_verify_script_agrees(self):
        result = subprocess.run(["bash", str(ROOT / "scripts" / "vendor-assets.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pages_reference_only_vendored_assets(self):
        import asyncio

        from inkcast import api
        swagger = asyncio.run(api.swagger_ui(None)).body.decode()
        redoc = asyncio.run(api.redoc(None)).body.decode()
        for page in (swagger, redoc):
            self.assertNotRegex(page, r"https?://", "docs pages must not load anything from outside the server")
        self.assertIn("/api/static/swagger-ui-bundle.js?v=", swagger)
        self.assertIn("/api/static/redoc.standalone.js?v=", redoc)

    def test_tampered_bundle_is_refused(self):
        from inkcast import api
        api._asset.cache_clear()
        api.vendor_lock.cache_clear()
        original = api.vendor_lock
        try:
            api.vendor_lock = lambda: {**original(), "swagger-ui.css": "0" * 64}
            with self.assertRaises(RuntimeError):
                api._asset("swagger-ui.css")
        finally:
            api.vendor_lock = original
            api._asset.cache_clear()


if __name__ == "__main__":
    unittest.main()
