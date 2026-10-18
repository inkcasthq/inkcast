"""Shared libraries: secrets handling, log redaction, the config gate, service naming, prefixes."""

import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from starlette.testclient import TestClient

from inkcast.launcher import EX_CONFIG, build, resolve
from inkcast.libs.config import Config, ConfigError, load_config
from inkcast.libs.http.app import compose
from inkcast.libs.observability import EVENTS, JsonFormatter, Redactor, TextFormatter, redact, request_id_var
from inkcast.libs.secrets import REDACTOR, SecretsProvider, SecretValue


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

    def capture(self, formatter):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(formatter)
        handler.addFilter(Redactor())
        log = logging.getLogger(f"inkcast.test.redaction.{id(formatter)}")
        log.addHandler(handler)
        log.propagate = False
        return log, stream

    def test_json_logs_carry_the_service_and_are_redacted(self):
        log, stream = self.capture(JsonFormatter("PublishingApi"))
        log.warning("token was %s", "ic_rt_SECRETSECRETSECRET")
        line = stream.getvalue()
        self.assertIn('"service": "PublishingApi"', line)
        self.assertNotIn("SECRETSECRETSECRET", line)

    def test_registered_values_are_masked_in_fields_messages_and_tracebacks_in_both_formats(self):
        REDACTOR.register("kc-secret-value-0042")
        for formatter in (JsonFormatter("IdentityService"), TextFormatter("%(levelname)s %(message)s")):
            log, stream = self.capture(formatter)
            token = request_id_var.set("req-abcdef123456")
            try:
                try:
                    raise RuntimeError("POST https://sso/token failed: client_secret=kc-secret-value-0042 rejected")
                except RuntimeError:
                    log.exception("login failed for %s", "kc-secret-value-0042",
                                  extra={"event": "auth.login.refused", "attempt": 3, "note": "x kc-secret-value-0042 y"})
            finally:
                request_id_var.reset(token)
            out = stream.getvalue()
            self.assertNotIn("kc-secret-value-0042", out)
            self.assertIn("[REDACTED]", out)
            self.assertIn("req-abcdef123456", out)
            self.assertIn("auth.login.refused", out)
            self.assertIn("RuntimeError", out)  # the traceback is still there, scrubbed
        line = json.loads(out.strip()) if out.startswith("{") else None
        if line:
            self.assertEqual((line["event"], line["attempt"], line["request_id"]), ("auth.login.refused", 3, "req-abcdef123456"))

    def test_every_event_name_is_catalogued_and_used(self):
        import pathlib
        import re
        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "inkcast"
        used = set()
        for path in src.rglob("*.py"):
            used |= set(re.findall(r"""["']event["']\s*:\s*["']([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)["']""", path.read_text()))
        self.assertEqual(used - set(EVENTS), set(), "events logged but not in observability.EVENTS")
        self.assertEqual(set(EVENTS) - used, set(), "events catalogued but never logged")


class ConfigGate(unittest.TestCase):
    def cfg(self, **kw):
        return Config(**{"public_url": "http://localhost:8787", "secret_key": "k" * 40, "database_path": ":memory:", **kw})

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

    def test_unreadable_secret_files_only_stop_the_services_that_need_them(self):
        # The shared ConfigMap sets INKCAST_PASSWORD_FILE for every pod; the Gateway mounts no secrets and must start.
        env = {"INKCAST_PUBLIC_URL": "http://localhost:8787", "INKCAST_LOGIN": "password",
               "INKCAST_PASSWORD_FILE": "/var/run/secrets/inkcast/nope-password",
               "INKCAST_SECRET_KEY_FILE": "/var/run/secrets/inkcast/nope-key",
               "INKCAST_UPSTREAM_PUBLISHING_API": "http://publishing-api:8787"}
        cfg = load_config(env)
        self.assertEqual(sorted(cfg.secret_errors), ["INKCAST_PASSWORD", "INKCAST_SECRET_KEY"])
        cfg.validate(resolve("Gateway")[0].needs)                         # no secrets needed: starts
        with self.assertRaisesRegex(ConfigError, r"INKCAST_SECRET_KEY can't be read.*nope-key") as caught:
            cfg.validate(resolve("PublishingApi")[0].needs)                # needs the key, not the password
        self.assertNotIn("INKCAST_PASSWORD", str(caught.exception))
        with self.assertRaisesRegex(ConfigError, r"INKCAST_PASSWORD can't be read.*nope-password"):
            cfg.validate(resolve("IdentityService")[0].needs)
        self.assertEqual(sorted(s["name"] for s in cfg.secrets.status()), ["GITHUB_CLIENT_SECRET", "OIDC_CLIENT_SECRET"])

    def test_secrets_flow_through_the_provider_and_never_print(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.env"), "w") as f:
                f.write("INKCAST_PUBLIC_URL=https://file.example.com\nINKCAST_METRICS_PORT=0\nINKCAST_ENV=staging\n")
            os.makedirs(os.path.join(d, "mnt"))
            with open(os.path.join(d, "mnt", "secret-key"), "w") as f:
                f.write("s" * 40)
            env = {"INKCAST_CONFIG_FILE": os.path.join(d, "config.env"), "INKCAST_PUBLIC_URL": "https://env.example.com",
                   "INKCAST_SECRETS_DIR": os.path.join(d, "mnt"), "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
            cfg = load_config(env)
            self.assertEqual(cfg.public_url, "https://env.example.com")      # the real environment wins over the file
            self.assertEqual((cfg.metrics_port, cfg.environment), (0, "staging"))
            self.assertEqual(cfg.secret_key, "s" * 40)
            self.assertEqual(cfg.secret_key.reveal(), "s" * 40)
            self.assertNotIn("ssss", repr(cfg))
            self.assertNotIn("pppp", repr(cfg))
            self.assertEqual(cfg.secrets.mode, "env+dir")
            by_name = {s["name"]: s for s in cfg.secrets.status()}
            self.assertTrue(by_name["INKCAST_SECRET_KEY"]["source"].startswith("file:"))
            self.assertEqual(by_name["INKCAST_PASSWORD"]["source"], "env")
            self.assertEqual(by_name["OIDC_CLIENT_SECRET"]["source"], "absent")
            self.assertNotIn("ssss", str(cfg.secrets.status()))
            cfg.validate()
            with self.assertRaisesRegex(ConfigError, "INKCAST_METRICS_PORT must be a whole number"):
                load_config({**env, "INKCAST_METRICS_PORT": "nine"})
            with self.assertRaisesRegex(ConfigError, "looks like a credential"):
                with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
                    f.write("INKCAST_SECRET_KEY: " + "x" * 40 + "\n")
                load_config({**env, "INKCAST_CONFIG_FILE": f.name})
            with self.assertRaisesRegex(ConfigError, "INKCAST_ENV must be one of"):
                load_config({**env, "INKCAST_ENV": "prod"}).validate()
            with self.assertRaisesRegex(ConfigError, "INKCAST_LOG_REVEAL_SECRETS must not"):
                load_config({**env, "INKCAST_ENV": "production", "INKCAST_LOG_REVEAL_SECRETS": "true"}).validate()
            with self.assertRaisesRegex(ConfigError, "not available in this build"):
                load_config({**env, "INKCAST_SECRETS_SOURCE": "vault", "INKCAST_SECRETS_ENV": "dev"})

    def test_the_gate_covers_the_posture_checks_and_skips_what_a_service_does_not_use(self):
        from inkcast.launcher import gate
        gw = self.cfg(secret_key="", upstreams={"publishing-api": "http://publishing-api:8787"})
        gate(gw, resolve("Gateway"))                                           # no key, no problem
        with self.assertRaisesRegex(ConfigError, "INKCAST_SECRET_KEY must be at least 32"):
            gate(gw, resolve("PublishingApi"))
        weak = self.cfg(secret_key="k" * 48, environment="production", public_url="https://inkcast.example.com")
        with self.assertRaisesRegex(ConfigError, "(?s)Security posture problems.*not a random key"):
            gate(weak, resolve("PublishingApi"))
        with self.assertLogs("inkcast", "WARNING") as logs:                   # the same finding only warns elsewhere
            gate(self.cfg(secret_key="k" * 48), resolve("PublishingApi"))
        self.assertTrue(any("not a random key" in line for line in logs.output))

    def test_bad_config_exits_78_before_serving(self):
        env = {**os.environ, "PYTHONPATH": "src", "INKCAST_APP_NAME": "IdentityService", "INKCAST_LOGIN": "oidc",
               "INKCAST_SECRET_KEY": "short", "INKCAST_METRICS_PORT": "0", "INKCAST_LOG_FORMAT": "json"}
        r = subprocess.run([sys.executable, "-m", "inkcast"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, EX_CONFIG)
        self.assertIn("INKCAST_SECRET_KEY must be at least 32", r.stderr)
        self.assertIn("OIDC_ISSUER", r.stderr)
        self.assertIn('"event": "config.unsafe"', r.stdout)
        r = subprocess.run([sys.executable, "-m", "inkcast", "run", "publishingApi"], env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, EX_CONFIG)
        self.assertIn("CamelCase", r.stderr)
        # Reading the configuration is behind the same gate: a bad number is exit 78, not a traceback.
        r = subprocess.run([sys.executable, "-m", "inkcast"], env={**env, "INKCAST_METRICS_PORT": "nine"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, EX_CONFIG)
        self.assertIn("INKCAST_METRICS_PORT must be a whole number", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        # `inkcast checks` prints the posture table without starting anything.
        r = subprocess.run([sys.executable, "-m", "inkcast", "checks"], env={**env, "INKCAST_SECRET_KEY": "k" * 40},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("secret-key-strength", r.stdout)
        self.assertNotIn("kkkk", r.stdout)


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
