"""Shared libraries: secrets handling, log redaction, the config gate, service naming, prefixes."""

import io
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from starlette.testclient import TestClient

from inkcast.launcher import EX_CONFIG, build, resolve
from inkcast.libs.config import Config, ConfigError
from inkcast.libs.http.app import compose
from inkcast.libs.observability import JsonFormatter, Redactor, redact
from inkcast.libs.secrets import SecretsProvider, SecretValue


class Secrets(unittest.TestCase):
    def test_secret_value_never_prints(self):
        s = SecretValue("hunter2-hunter2")
        self.assertEqual(repr(s), "SecretValue('***')")
        self.assertNotIn("hunter2", f"{s} {[s]} {dict(k=s)}")
        self.assertEqual(s.reveal(), "hunter2-hunter2")
        self.assertEqual(s, SecretValue("hunter2-hunter2"))

    def test_provider_reads_files_caches_and_survives_a_failed_reread(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "key")
            with open(path, "w") as f:
                f.write("v1\n")
            p = SecretsProvider(ttl=60, environ={"KEY_FILE": path}, clock=lambda: now[0])
            self.assertEqual(p.get("KEY").reveal(), "v1")
            with open(path, "w") as f:
                f.write("v2")
            self.assertEqual(p.get("KEY").reveal(), "v1")       # cached
            now[0] = 100
            self.assertEqual(p.get("KEY").reveal(), "v2")       # rotated value picked up after the TTL
            os.remove(path)
            now[0] = 200
            with self.assertLogs("inkcast.libs.secrets", "WARNING"):
                self.assertEqual(p.get("KEY").reveal(), "v2")   # stale-on-error, not an outage
        with self.assertRaises(OSError):
            SecretsProvider(environ={"NEW_FILE": "/nonexistent/x"}).get("NEW")

    def test_provider_reads_once_under_concurrency(self):
        reads = []
        p = SecretsProvider(environ={"K": "v"})
        original = p._read
        p._read = lambda name: (reads.append(name), original(name))[1]
        threads = [threading.Thread(target=p.get, args=("K",)) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(reads), 1)


class Logging(unittest.TestCase):
    def test_tokens_and_keys_are_redacted(self):
        text = ("Authorization: Bearer abcdefghijklmnop ink_pat_1a2b3c4d5e6f_SECRETSECRETSECRET "
                "ic_at_SECRETSECRETSECRET url?code=abc123&state=x api_key=k-123 client_secret=zzz")
        out = redact(text)
        for secret in ("abcdefghijklmnop", "SECRETSECRETSECRET", "abc123", "k-123", "zzz"):
            self.assertNotIn(secret, out)
        self.assertIn("ink_pat_1a2b3c4d5e6f_***", out)   # the id part stays, so you can tell which token
        self.assertIn("state=x", out)

    def test_json_logs_carry_the_service_and_are_redacted(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter("PublishingApi"))
        handler.addFilter(Redactor())
        log = logging.getLogger("inkcast.test.redaction")
        log.addHandler(handler)
        log.propagate = False
        log.warning("token was %s", "ic_rt_SECRETSECRETSECRET")
        line = stream.getvalue()
        self.assertIn('"service": "PublishingApi"', line)
        self.assertNotIn("SECRETSECRETSECRET", line)


class ConfigGate(unittest.TestCase):
    def cfg(self, **kw):
        return Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", **kw)

    def test_each_service_checks_only_what_it_needs(self):
        c = self.cfg(login="oidc")                         # no OIDC settings at all
        with self.assertRaisesRegex(ConfigError, "OIDC_ISSUER"):
            c.validate(resolve("IdentityService")[0].needs)
        c.validate(resolve("PublishingApi")[0].needs)      # doesn't need sign-in settings
        c.validate(resolve("SchedulerWorker")[0].needs)
        with self.assertRaisesRegex(ConfigError, "INKCAST_UPSTREAM_"):
            c.validate(resolve("Gateway")[0].needs)
        with self.assertRaisesRegex(ConfigError, "http"):
            self.cfg(upstreams={"publishing-api": "ftp://x"}).validate(["upstreams"])
        with self.assertRaisesRegex(ConfigError, "INKCAST_PATH_PREFIX"):
            self.cfg(path_prefix="nope").validate([])

    def test_bad_config_exits_78_before_serving(self):
        env = {**os.environ, "PYTHONPATH": "src", "INKCAST_APP_NAME": "IdentityService", "INKCAST_LOGIN": "oidc",
               "INKCAST_SECRET_KEY": "short", "INKCAST_METRICS_PORT": "0"}
        r = subprocess.run([sys.executable, "-m", "inkcast"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, EX_CONFIG)
        self.assertIn("INKCAST_SECRET_KEY must be at least 32", r.stderr)
        self.assertIn("OIDC_ISSUER", r.stderr)
        r = subprocess.run([sys.executable, "-m", "inkcast", "run", "publishingApi"], env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, EX_CONFIG)
        self.assertIn("CamelCase", r.stderr)


class Prefixes(unittest.TestCase):
    def app(self, names: str, prefix: str):
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:",
                     login="password", password="p" * 12, path_prefix=prefix)
        specs = resolve(names)
        return TestClient(compose(specs, build(cfg, specs)), base_url="http://localhost:8787")

    def test_root_auto_and_explicit_prefixes(self):
        with self.app("ConnectorService", "/") as c:
            self.assertEqual(c.get("/connect/nope").status_code, 400)
        with self.app("ConnectorService", "auto") as c:
            self.assertEqual(c.get("/connector-service/connect/nope").status_code, 400)
            self.assertEqual(c.get("/connect/nope").status_code, 404)
            self.assertEqual(c.get("/livez").status_code, 200)
        with self.app("ConnectorService,IdentityService", "auto") as c:
            self.assertEqual(c.get("/identity-service/.well-known/oauth-authorization-server").status_code, 200)
            self.assertEqual(c.get("/connector-service/connect/nope").status_code, 400)
        with self.app("IdentityService", "/inkcast") as c:
            self.assertEqual(c.get("/inkcast/.well-known/oauth-authorization-server").status_code, 200)
            ready = c.get("/readyz").json()
            self.assertEqual(ready["services"], ["IdentityService"])


if __name__ == "__main__":
    unittest.main()
