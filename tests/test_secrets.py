"""The secrets package: SecretValue, value-based redaction, masking."""

import copy
import json
import pickle
import unittest

from inkcast.libs.secrets import Redactor, SecretValue, Sealer, describe_keys, fingerprint, partly_mask
from inkcast.libs.secrets.redactor import is_sensitive_key


class SecretValueTests(unittest.TestCase):
    def test_never_prints_and_compares_in_constant_time(self):
        s = SecretValue("hunter2-hunter2")
        self.assertEqual(repr(s), "SecretValue('***')")
        self.assertNotIn("hunter2", f"{s} {s!r} {s!s} {[s]} {dict(k=s)} {s:>20}")
        self.assertNotIn("hunter2", "%s %r" % (s, s))
        self.assertEqual(s.reveal(), "hunter2-hunter2")
        self.assertEqual(s, SecretValue("hunter2-hunter2"))
        self.assertEqual(s, "hunter2-hunter2")                 # plain strings compare too (constant time)
        self.assertNotEqual(s, "hunter2-hunter3")
        self.assertNotEqual(s, 42)
        self.assertTrue(s)
        self.assertFalse(SecretValue(""))
        self.assertEqual(SecretValue(s).reveal(), s.reveal())   # idempotent wrap
        with self.assertRaises(TypeError):
            SecretValue(123)  # type: ignore[arg-type]

    def test_refuses_the_accidental_serialisations(self):
        s = SecretValue("hunter2-hunter2")
        with self.assertRaises(TypeError):
            json.dumps({"k": s})
        with self.assertRaises(TypeError):
            pickle.dumps(s)
        self.assertIs(copy.deepcopy(s), s)
        self.assertEqual(hash(s), hash(SecretValue("hunter2-hunter2")))

    def test_sealer_accepts_plain_or_wrapped_keys(self):
        a, b = Sealer("k" * 40), Sealer(SecretValue("k" * 40))
        self.assertEqual(b.unseal(a.seal({"api_key": "x"})), {"api_key": "x"})
        with self.assertRaisesRegex(RuntimeError, "INKCAST_SECRET_KEY changed"):
            Sealer("j" * 40).unseal(a.seal({"api_key": "x"}))


class RedactorTests(unittest.TestCase):
    def test_registered_values_are_masked_in_any_encoding(self):
        r = Redactor()
        r.register("p@ss/word+123")
        r.register("short")  # too short to register safely
        self.assertEqual(r.size, 2)  # raw and URL-encoded forms (the JSON form is the same here)
        r.register('quo"te-value')
        self.assertTrue(r.contains('{"pw": "quo\\"te-value"}'))  # the JSON-escaped form
        text = 'connect postgres://app:p@ss/word+123@db failed; q=p%40ss%2Fword%2B123'
        out = r.scrub(text)
        self.assertNotIn("p@ss", out)
        self.assertNotIn("p%40ss", out)
        self.assertIn("[REDACTED]", out)
        self.assertTrue(r.contains(text))
        self.assertFalse(r.contains("nothing here"))
        self.assertEqual(r.scrub("https://u:topsecretpw@host/x"), "https://u:[REDACTED]@host/x")  # even unregistered

    def test_register_mapping_takes_credential_leaves_only(self):
        r = Redactor()
        r.register_mapping({"username": "alice-in-wonderland", "password": "wonderland-42", "nested": {"api_key": "key-1234567"}},
                           raw='{"password": "wonderland-42"}')
        self.assertTrue(r.contains("wonderland-42"))
        self.assertTrue(r.contains("key-1234567"))
        self.assertFalse(r.contains("alice-in-wonderland"))

    def test_scrub_value_blanks_sensitive_keys_and_reduces_errors(self):
        r = Redactor()
        r.register("hunter2-hunter2")
        out = r.scrub_value({"password": "x", "client_secret": "", "note": "pw hunter2-hunter2", "err": ValueError("hunter2-hunter2"),
                             "secret": SecretValue("abc"), "items": ["hunter2-hunter2"]})
        self.assertEqual(out["password"], "[REDACTED]")
        self.assertEqual(out["client_secret"], "")
        self.assertEqual(out["note"], "pw [REDACTED]")
        self.assertEqual(out["err"], {"type": "ValueError", "message": "[REDACTED]"})
        self.assertEqual(out["secret"], "[REDACTED]")
        self.assertEqual(out["items"], ["[REDACTED]"])
        self.assertTrue(is_sensitive_key("OIDC_CLIENT_SECRET") and is_sensitive_key("x-vault-token"))
        self.assertTrue(is_sensitive_key("INKCAST_SECRET_KEY") and is_sensitive_key("token_hash") is False)
        self.assertFalse(is_sensitive_key("host") or is_sensitive_key("INKCAST_SECRET_KEY_FILE") or is_sensitive_key("INKCAST_SECRETS_DIR"))

    def test_remembers_a_bounded_number_of_values(self):
        r = Redactor()
        for i in range(300):
            r.register(f"value-number-{i:04d}")
        self.assertLessEqual(r.size, 200)
        self.assertTrue(r.contains("value-number-0299"))
        self.assertFalse(r.contains("value-number-0000"))


class MaskTests(unittest.TestCase):
    def test_describe_keys_shows_names_and_fingerprints_never_values(self):
        key = b"k" * 32
        lines = describe_keys({"INKCAST_SECRET_KEY": "s" * 48, "host": "db.internal", "username": "northwind-app",
                               "OIDC_CLIENT_SECRET": SecretValue("kc-secret-value")}, key)
        self.assertEqual(lines[0], f"INKCAST_SECRET_KEY=******** (48 chars, fp={fingerprint('s' * 48, key)})")
        self.assertEqual(lines[1], "host=db.internal")
        self.assertEqual(lines[2], "username=no*********pp")
        self.assertIn("(15 chars, fp=", lines[3])
        self.assertNotIn("kc-secret", " ".join(lines))
        self.assertNotEqual(fingerprint("a" * 20, key), fingerprint("a" * 20, b"j" * 32))  # key-dependent
        self.assertEqual(partly_mask("ab"), "**")
        self.assertEqual(partly_mask("abcdef"), "a*****")


if __name__ == "__main__":
    unittest.main()


class NamingTests(unittest.TestCase):
    def test_pattern_rendering_and_validation(self):
        from inkcast.libs.secrets.naming import NamingError, SecretNaming, render_secret_name, secret_slug
        self.assertEqual(secret_slug("INKCAST_SECRET_KEY"), "secret-key")
        self.assertEqual(secret_slug("OIDC_CLIENT_SECRET"), "oidc-client-secret")
        values = {"app": "inkcast", "env": "prod", "service": "identity-service", "name": "secret-key"}
        self.assertEqual(render_secret_name("app-env-name", values), "/inkcast/prod/secret-key")
        self.assertEqual(render_secret_name("/{env}/{app}/{service}/{name}", values), "/prod/inkcast/identity-service/secret-key")
        for pattern, message in (("/{app}/{name}", "must contain {env}"), ("/{app}/{env}/{nope}", "unknown token"),
                                 ("/{app}/{env", "unbalanced"), ("/{app}/{env}/{region}", "{region}, which is not set")):
            with self.assertRaisesRegex(NamingError, message):
                render_secret_name(pattern, values)
        with self.assertRaisesRegex(NamingError, "no '/'"):
            render_secret_name("/{app}/{env}/{name}", {**values, "env": "pr/od"})

        naming = SecretNaming.from_environ({"INKCAST_SECRETS_ENV": "prod", "INKCAST_SECRET_ID_OIDC_CLIENT_SECRET": "arn:aws:x"},
                                           service="identity-service")
        self.assertEqual(naming.remote_id("INKCAST_SECRET_KEY"), "/inkcast/prod/secret-key")
        self.assertEqual(naming.remote_id("OIDC_CLIENT_SECRET"), "arn:aws:x")      # explicit id wins
        with self.assertRaisesRegex(NamingError, "not one of dev, staging, prod"):
            SecretNaming.from_environ({"INKCAST_SECRETS_ENV": "uat"}).remote_id("X")
        self.assertEqual(SecretNaming.from_environ({"INKCAST_SECRETS_ENV": "uat", "INKCAST_SECRETS_ALLOWED_ENVS": "uat,prod"})
                         .remote_id("INKCAST_PASSWORD"), "/inkcast/uat/password")
        with self.assertRaisesRegex(NamingError, "INKCAST_SECRETS_ENV"):
            SecretNaming.from_environ({}).remote_id("INKCAST_SECRET_KEY")
        self.assertIn("like the suffix AWS adds", SecretNaming.from_environ({"INKCAST_SECRETS_ENV": "dev"})
                      .warnings(("INKCAST_KEY_ABCDEF",))[0])

    def test_config_files_hold_names_never_values(self):
        import os
        import tempfile
        from inkcast.libs.secrets.naming import ConfigFileError, load_config_file
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "config.env")
            with open(good, "w") as f:
                f.write("# comment\nexport INKCAST_PUBLIC_URL=https://x.example.com  # trailing\nINKCAST_SECRET_KEY_FILE='/run/k'\n")
            self.assertEqual(load_config_file(good), {"INKCAST_PUBLIC_URL": "https://x.example.com", "INKCAST_SECRET_KEY_FILE": "/run/k"})
            yml = os.path.join(d, "config.yaml")
            with open(yml, "w") as f:
                f.write("INKCAST_LOGIN: oidc\nINKCAST_METRICS_PORT: 9090\nINKCAST_ALLOW_ANY_USER: true\n")
            self.assertEqual(load_config_file(yml), {"INKCAST_LOGIN": "oidc", "INKCAST_METRICS_PORT": "9090",
                                                     "INKCAST_ALLOW_ANY_USER": "true"})
            bad = os.path.join(d, "bad.yaml")
            with open(bad, "w") as f:
                f.write("INKCAST_SECRET_KEY: abcdefghijklmnopqrstuvwxyz0123456789\n")
            with self.assertRaisesRegex(ConfigFileError, "looks like a credential"):
                load_config_file(bad)
            with open(bad, "w") as f:
                f.write('{"DATABASE_URL": "postgres://u:pw@db/x"}')
            with self.assertRaisesRegex(ConfigFileError, "URL with a password"):
                load_config_file(os.path.join(d, "bad.yaml"))
            with open(os.path.join(d, "nested.json"), "w") as f:
                f.write('{"oidc": {"issuer": "x"}}')
            with self.assertRaisesRegex(ConfigFileError, "scalar"):
                load_config_file(os.path.join(d, "nested.json"))
            with open(os.path.join(d, "x.toml"), "w") as f:
                f.write("a = 1\n")
            with self.assertRaisesRegex(ConfigFileError, "unsupported"):
                load_config_file(os.path.join(d, "x.toml"))
            with self.assertRaisesRegex(ConfigFileError, "can't be read"):
                load_config_file(os.path.join(d, "missing.env"))


class FetcherTests(unittest.TestCase):
    def test_env_file_and_directory_sources(self):
        import os
        import tempfile
        from inkcast.libs.secrets.fetchers import ChainFetcher, DirectoryFetcher, EnvFetcher, fetcher_from_environ
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "mnt"))
            with open(os.path.join(d, "mnt", "secret-key"), "w") as f:
                f.write("from-the-volume\n")
            with open(os.path.join(d, "mnt", "OIDC_CLIENT_SECRET"), "w") as f:
                f.write("kc-from-volume")
            with open(os.path.join(d, "pw"), "w") as f:
                f.write("from-a-file\n")
            env = {"INKCAST_PASSWORD_FILE": os.path.join(d, "pw"), "GITHUB_CLIENT_SECRET": "from-env", "EMPTY": ""}
            e = EnvFetcher(env)
            self.assertEqual((e.fetch("INKCAST_PASSWORD").value, e.fetch("INKCAST_PASSWORD").source), ("from-a-file", f"file:{d}/pw"))
            self.assertIsNotNone(e.fetch("INKCAST_PASSWORD").version)
            self.assertEqual(e.fetch("GITHUB_CLIENT_SECRET").source, "env")
            self.assertIsNone(e.fetch("EMPTY"))
            self.assertIsNone(e.fetch("MISSING"))
            self.assertEqual(e.paths("INKCAST_PASSWORD"), [os.path.join(d, "pw")])
            dir_fetcher = DirectoryFetcher(os.path.join(d, "mnt"))
            self.assertEqual(dir_fetcher.fetch("INKCAST_SECRET_KEY").value, "from-the-volume")
            self.assertEqual(dir_fetcher.fetch("OIDC_CLIENT_SECRET").value, "kc-from-volume")
            self.assertIsNone(dir_fetcher.fetch("INKCAST_PASSWORD"))
            chain = fetcher_from_environ({**env, "INKCAST_SECRETS_DIR": os.path.join(d, "mnt")})
            self.assertIsInstance(chain, ChainFetcher)
            self.assertEqual(chain.kind, "env+dir")
            self.assertEqual(chain.fetch("INKCAST_SECRET_KEY").value, "from-the-volume")
            self.assertEqual(chain.fetch("INKCAST_PASSWORD").value, "from-a-file")  # env/_FILE comes first
            self.assertEqual(len(chain.paths("INKCAST_SECRET_KEY")), 1)
            self.assertIs(type(fetcher_from_environ({})), EnvFetcher)

    def test_backends_fail_clearly_when_not_available(self):
        from inkcast.libs.secrets.fetchers import NotInstalledError, SecretFetchError, fetcher_from_environ
        with self.assertRaisesRegex(NotInstalledError, "not available in this build"):
            fetcher_from_environ({"INKCAST_SECRETS_SOURCE": "vault", "INKCAST_SECRETS_ENV": "dev"})
        with self.assertRaisesRegex(SecretFetchError, "must be env, dir, agent, aws or vault"):
            fetcher_from_environ({"INKCAST_SECRETS_SOURCE": "s3"})
        with self.assertRaisesRegex(SecretFetchError, "needs INKCAST_SECRETS_DIR"):
            fetcher_from_environ({"INKCAST_SECRETS_SOURCE": "dir"})
        try:
            import boto3  # noqa: F401
        except ImportError:
            with self.assertRaisesRegex(NotInstalledError, "boto3"):
                fetcher_from_environ({"INKCAST_SECRETS_SOURCE": "aws", "INKCAST_SECRETS_ENV": "dev"})

    def test_agent_fetcher_talks_to_loopback_only_with_the_ssrf_token(self):
        import os
        import tempfile
        from inkcast.libs.secrets.fetchers import AgentFetcher, SecretFetchError
        from inkcast.libs.secrets.naming import SecretNaming

        class Resp:
            def __init__(self, status, body):
                self.status_code, self._body = status, body

            def json(self):
                return self._body

        class FakeHttp:
            calls: list = []
            answer = Resp(200, {"SecretString": '{"value": "agent-secret-1"}', "VersionId": "v1"})

            def get(self, url, params, headers):
                self.calls.append((url, params, headers))
                return self.answer

        naming = SecretNaming.from_environ({"INKCAST_SECRETS_ENV": "dev"})
        with self.assertRaisesRegex(SecretFetchError, "localhost"):
            AgentFetcher(naming, url="http://secrets.example.com:2773")
        with tempfile.TemporaryDirectory() as d:
            token_file = os.path.join(d, "token")
            with open(token_file, "w") as f:
                f.write("ssrf-token-value\n")
            http = FakeHttp()
            agent = AgentFetcher(naming, token_file=token_file, http=http)
            got = agent.fetch("INKCAST_SECRET_KEY", force=True)
            self.assertEqual((got.value, got.source, got.version), ("agent-secret-1", "agent:/inkcast/dev/secret-key", "v1"))
            url, params, headers = http.calls[-1]
            self.assertEqual((url, params), ("http://localhost:2773/secretsmanager/get",
                                             {"secretId": "/inkcast/dev/secret-key", "refreshNow": "true"}))
            self.assertEqual(headers["X-Aws-Parameters-Secrets-Token"], "ssrf-token-value")
            http.answer = Resp(200, {"SecretString": "plain-text-secret"})
            self.assertEqual(agent.fetch("INKCAST_SECRET_KEY").value, "plain-text-secret")
            http.answer = Resp(200, {"SecretString": '{"a": 1, "b": 2}'})
            with self.assertRaisesRegex(SecretFetchError, "several keys"):
                agent.fetch("INKCAST_SECRET_KEY")
            http.answer = Resp(404, {})
            self.assertIsNone(agent.fetch("INKCAST_SECRET_KEY"))
            http.answer = Resp(403, {})
            with self.assertRaisesRegex(SecretFetchError, "SSRF token was refused"):
                agent.fetch("INKCAST_SECRET_KEY")
            self.assertEqual(agent.paths("X"), [token_file])


class ProviderTests(unittest.TestCase):
    def make(self, value="v1", **kw):
        from inkcast.libs.secrets import Fetched, Fetcher, SecretsProvider

        class Fake(Fetcher):
            kind = "fake"

            def __init__(self):
                self.value, self.version, self.calls, self.fail, self.delay = value, 1, 0, False, 0.0

            def fetch(self, name, *, force=False):
                import time
                self.calls += 1
                self.last_force = force
                if self.delay:
                    time.sleep(self.delay)
                if self.fail:
                    raise RuntimeError("endpoint unreachable")
                return Fetched(self.value, f"fake:{name}", f"v{self.version}")

        now = [0.0]
        fetcher = Fake()
        redactor = Redactor()
        provider = SecretsProvider(fetcher, clock=lambda: now[0], redactor=redactor, **kw)
        return provider, fetcher, now, redactor

    def test_caches_registers_and_reports_status_without_values(self):
        provider, fetcher, now, redactor = self.make("pw-version-1", ttl=300)
        a, b = provider.get("DB"), provider.get("DB")
        self.assertEqual((a.reveal(), b.reveal(), fetcher.calls), ("pw-version-1", "pw-version-1", 1))
        self.assertTrue(redactor.contains("pw-version-1"))  # registered before anything can log it
        [s] = provider.status()
        self.assertEqual((s["name"], s["source"], s["version"], s["length"], s["cache_hits"], s["fetches"], s["present"]),
                         ("DB", "fake:DB", "v1", 12, 1, 1, True))
        self.assertNotIn("pw-version-1", json.dumps(provider.status()))
        self.assertEqual(provider.mode, "fake")
        self.assertEqual(provider.names(), ["DB"])

    def test_refresh_bypasses_the_cache_and_tells_listeners(self):
        provider, fetcher, now, _ = self.make("pw-version-1")
        events = []
        provider.on_change(events.append)
        provider.get("DB")
        fetcher.value, fetcher.version = "pw-version-2", 2
        self.assertEqual(provider.get("DB").reveal(), "pw-version-1")  # still cached
        self.assertEqual(provider.refresh("DB", "db-auth-failure").reveal(), "pw-version-2")
        self.assertTrue(fetcher.last_force)
        [event] = events
        self.assertEqual((event.name, event.reason, event.changed, event.version), ("DB", "db-auth-failure", True, "v2"))
        self.assertNotEqual(event.fingerprint, event.previous_fingerprint)
        self.assertEqual(provider.status()[0]["changes"], 1)
        provider.refresh("DB", "again")  # same value: no event
        self.assertEqual(len(events), 1)

    def test_stale_on_error_is_bounded_by_max_stale(self):
        provider, fetcher, now, _ = self.make("pw-version-1", ttl=100, max_stale=1000)
        provider.get("DB")
        now[0] = 200  # TTL expired
        fetcher.fail = True
        with self.assertLogs("inkcast.libs.secrets", "WARNING"):
            self.assertEqual(provider.get("DB").reveal(), "pw-version-1")  # served stale
        self.assertIn("endpoint unreachable", provider.status()[0]["last_error"])
        self.assertTrue(provider.status()[0]["stale"])
        self.assertEqual(provider.get("DB").reveal(), "pw-version-1")  # within the short retry window: no fetch
        self.assertEqual(fetcher.calls, 2)
        now[0] = 1300  # older than max_stale: the outage is now an error
        with self.assertRaises(RuntimeError):
            provider.get("DB")
        with self.assertRaises(RuntimeError):  # a forced refresh never serves stale
            provider.refresh("DB")
        fetcher.fail = False
        self.assertEqual(provider.get("DB").reveal(), "pw-version-1")
        self.assertIsNone(provider.status()[0]["last_error"])

    def test_single_flight_under_concurrency(self):
        import threading
        provider, fetcher, now, _ = self.make("pw")
        fetcher.delay = 0.05
        threads = [threading.Thread(target=provider.get, args=("DB",)) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(fetcher.calls, 1)

    def test_absent_secrets_are_empty_and_require_raises(self):
        from inkcast.libs.secrets import SecretNotFound, SecretsProvider
        p = SecretsProvider(environ={"PRESENT": "value-here"})
        self.assertFalse(p.get("MISSING"))
        self.assertEqual(p.status()[0]["source"], "absent")
        with self.assertRaises(SecretNotFound):
            p.require("MISSING")
        self.assertEqual(p.require("PRESENT").reveal(), "value-here")

    def test_async_wrappers(self):
        import asyncio
        provider, fetcher, now, _ = self.make("pw")

        async def run():
            v = await provider.aget("DB")
            w = await provider.arefresh("DB", "test")
            return v.reveal(), w.reveal(), fetcher.calls
        self.assertEqual(asyncio.run(run()), ("pw", "pw", 2))


class PostureCheckTests(unittest.TestCase):
    def checks(self, environ=None, **cfg):
        import io
        from inkcast.libs.config import Config
        from inkcast.libs.secrets import run_checks
        from inkcast.libs.secrets.checks import render
        base = dict(public_url="https://inkcast.example.com", secret_key="Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE", database_path=":memory:")
        config = Config(**{**base, **cfg})
        out = run_checks(config, redactor=Redactor(), environ=environ or {})
        render(out, io.StringIO())
        return {c.id: c for c in out}

    def test_hard_failures_and_production_tightening(self):
        from inkcast.libs.secrets import problems
        c = self.checks(public_url="http://inkcast.example.com", secret_key="short")
        self.assertEqual((c["public-url-https"].status, c["secret-key-strength"].status), ("fail", "fail"))
        fails, _ = problems(list(c.values()))
        self.assertEqual({f.id for f in fails}, {"public-url-https", "secret-key-strength"})
        self.assertEqual(self.checks(public_url="http://localhost:8787")["public-url-https"].status, "info")
        self.assertEqual(self.checks(secret_key="k" * 48)["secret-key-strength"].status, "warn")       # not random
        self.assertEqual(self.checks(secret_key="k" * 48, environment="production")["secret-key-strength"].status, "fail")
        self.assertEqual(self.checks(secret_key="changeme-" * 5)["secret-key-strength"].status, "warn")
        self.assertEqual(self.checks()["secret-key-strength"].status, "pass")
        self.assertEqual(self.checks(reveal_secrets=True)["no-reveal-flag"].status, "warn")
        self.assertEqual(self.checks(reveal_secrets=True, environment="production")["no-reveal-flag"].status, "fail")
        self.assertEqual(self.checks(environment="production", log_format="text")["log-format-json"].status, "warn")
        self.assertEqual(self.checks(environment="production", log_level="DEBUG")["debug-flags"].status, "warn")
        self.assertEqual(self.checks(environment="production", login="password", password="p" * 12)["login-method"].status, "warn")
        self.assertEqual(self.checks({"INKCAST_FORWARDED_ALLOW_IPS": "*"})["forwarded-ips"].status, "info")

    def test_environment_and_file_posture_never_mention_values(self):
        import os
        import stat
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            key_file = os.path.join(d, "secret-key")
            with open(key_file, "w") as f:
                f.write("v" * 40)
            os.chmod(key_file, 0o644)
            env = {"INKCAST_SECRET_KEY_FILE": key_file, "GITHUB_CLIENT_SECRET": "gh-secret-in-env-0123", "INKCAST_LOGIN": "oidc",
                   "INKCAST_ALLOW_ANY_USER": "true", "INKCAST_SECRET_ID_X": "arn:aws:x"}
            c = self.checks(env)
            self.assertEqual(c["secret-files-protected"].status, "warn")
            self.assertIn(key_file, c["secret-files-protected"].detail)
            self.assertEqual(c["env-scrubbed"].status, "warn")
            self.assertIn("GITHUB_CLIENT_SECRET", c["env-scrubbed"].detail)
            self.assertNotIn("gh-secret-in-env", c["env-scrubbed"].detail)
            self.assertNotIn("INKCAST_SECRET_KEY_FILE", c["env-scrubbed"].detail)   # a pointer, not a value
            self.assertNotIn("INKCAST_ALLOW_ANY_USER", c["env-scrubbed"].detail)   # a switch
            os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
            c = self.checks({"INKCAST_SECRET_KEY_FILE": key_file})
            self.assertEqual((c["secret-files-protected"].status, c["env-scrubbed"].status), ("pass", "pass"))
            self.assertEqual(c["redaction-active"].status, "warn")  # a fresh Redactor with nothing registered
            self.assertIn(c["non-root"].status, ("pass", "warn"))
            self.assertIn(c["core-dumps"].status, ("pass", "warn", "skip"))
            self.assertEqual(c["secrets-source"].status, "skip")

    def test_checks_see_the_provider(self):
        from inkcast.libs.config import load_config
        from inkcast.libs.secrets import run_checks
        env = {"INKCAST_PUBLIC_URL": "http://localhost:1", "INKCAST_SECRET_KEY": "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
               "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12, "INKCAST_METRICS_PORT": "0"}
        config = load_config(env)
        by_id = {c.id: c for c in run_checks(config, environ=env)}
        self.assertEqual(by_id["redaction-active"].status, "pass")
        self.assertIn("Mode env", by_id["secrets-source"].detail)
        self.assertIn("not set: GITHUB_CLIENT_SECRET, OIDC_CLIENT_SECRET", by_id["secrets-source"].detail)
        self.assertIn("INKCAST_PASSWORD", by_id["env-scrubbed"].detail)
        self.assertNotIn("pppp", " ".join(c.detail for c in by_id.values()))
