# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of secrets, config and the platform edges: each test proves one finding."""

import asyncio
import io
import logging
import secrets
import unittest

from inkcast.libs.http.middleware import RequestContext
from inkcast.libs.observability import JsonFormatter, Redactor as RedactingFilter


def _json_lines(logger_name: str, level: str = "INFO") -> tuple[io.StringIO, logging.Logger]:
    """A logger wired like setup_logging(): the redacting filter on the handler, the JSON formatter."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger(logger_name)
    old = (logger.handlers[:], logger.level, logger.propagate)
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False

    def restore() -> None:
        logger.handlers[:], logger.level, logger.propagate = old[0], old[1], old[2]
    buf.restore = restore  # type: ignore[attr-defined]
    return buf, logger


async def _request(app, method: str, path: str) -> None:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    await app({"type": "http", "method": method, "path": path, "headers": [], "client": ("203.0.113.9", 4711)},
              receive, send)


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class ConnectTicketNotInAccessLog(unittest.TestCase):
    """The connect ticket is a one-time bearer secret (stored only as a hash, valid 15 minutes, binds credentials
    to an account). The access log line of every request carries the path, so the ticket was written in clear."""

    def test_the_access_log_masks_the_connect_ticket(self):
        buf, _ = _json_lines("inkcast.http")
        self.addCleanup(buf.restore)
        ticket = secrets.token_urlsafe(32)               # what VaultRepo.create_ticket mints
        asyncio.run(_request(RequestContext(_ok), "GET", f"/connect/{ticket}"))
        asyncio.run(_request(RequestContext(_ok), "POST", f"/connector-service/connect/{ticket}"))  # prefix "auto"
        out = buf.getvalue()
        self.assertIn('"event": "http.request"', out)
        self.assertNotIn(ticket, out)
        self.assertIn("/connect/", out)                  # the line still says which page it was


class MethodLabelStaysClosed(unittest.TestCase):
    """uvicorn's h11 parser accepts any token as a method (up to its 16 KB request-line limit), and the RED histogram
    was labelled with it verbatim: every unauthenticated request with a new method made a new series (14 bucket
    lines each) that the process kept forever and served on every scrape."""

    def test_unknown_methods_share_one_label(self):
        from inkcast.libs.http.middleware import HTTP_DURATION
        from inkcast.libs.observability import Metrics

        metrics = Metrics()
        metrics.histogram(HTTP_DURATION, "test")
        app = RequestContext(_ok, metrics=metrics)
        for i in range(25):
            asyncio.run(_request(app, f"X{'A' * 200}{i}", "/nope"))
        asyncio.run(_request(app, "GET", "/nope"))
        text = metrics.render()
        self.assertEqual(text.count(f"{HTTP_DURATION}_count{{"), 2)   # GET and one shared series for the rest
        self.assertFalse("AAAA" in text, "a client-chosen method reached a label")
        self.assertIn('method="GET"', text)


_FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
case "$*" in
  *imagePullPolicy*|*securityContext*) echo "" ;;
  *"get cronjob"*) echo "ghcr.io/inkcasthq/inkcast@sha256:0123" ;;
  *"get deploy inkcast"*) echo "1" ;;
  *"wait --for=delete"*) if [ -n "$FAKE_POD" ]; then echo "timed out waiting for the condition" >&2; exit 1; fi ;;
  *"get pod"*) [ -n "$FAKE_POD" ] && echo "$FAKE_POD" ;;
  *"apply -f -"*) cat > /dev/null ;;
esac
exit 0
"""


class RestoreScriptWaitsForTheServer(unittest.TestCase):
    """scripts/ops/restore.sh scales the server to 0, waits for its pod with `|| true`, then runs `inkcast restore
    --force`, which skips the library's live-server guard. When the pod outlives the wait (stuck terminating, slow
    shutdown, an API hiccup) the database was replaced under a running server: its later writes went to the file
    moved aside."""

    def run_script(self, pod: str):
        import os
        import pathlib
        import subprocess
        import tempfile
        root = pathlib.Path(__file__).resolve().parents[1]
        d = pathlib.Path(tempfile.mkdtemp())
        fake, calls = d / "kubectl", d / "calls"
        fake.write_text(_FAKE_KUBECTL)
        fake.chmod(0o755)
        env = {**os.environ, "KUBECTL": str(fake), "CALLS": str(calls), "FAKE_POD": pod}
        done = subprocess.run(["bash", str(root / "scripts/ops/restore.sh"), "inkcast"], env=env,
                              capture_output=True, text=True, timeout=60)
        return done.returncode, calls.read_text().splitlines()

    def test_no_forced_restore_while_the_server_pod_is_still_there(self):
        code, calls = self.run_script(pod="pod/inkcast-6d9f-abcde")
        self.assertFalse(any(c.startswith("-n inkcast apply") for c in calls), "the restore Job ran under a live pod")
        self.assertNotEqual(code, 0)
        self.assertEqual(calls[-1], "-n inkcast scale deploy/inkcast --replicas=1")   # the server is back as it was

    def test_a_stopped_server_is_still_restored(self):
        code, calls = self.run_script(pod="")
        self.assertEqual(code, 0)
        self.assertTrue(any(c.startswith("-n inkcast apply") for c in calls))


class EverySecretFileIsChecked(unittest.TestCase):
    """The secret-files-protected posture check looked at a hand-kept list of five credentials; the two added later
    (the Keycloak sync secret, OpenFGA's preshared key) could sit in a world-readable file and the check passed."""

    def test_a_world_readable_file_is_flagged_for_every_credential(self):
        import os
        import tempfile
        from inkcast.libs.config import SECRET_SETTINGS, Config
        from inkcast.libs.secrets import Redactor, run_checks
        config = Config(public_url="https://inkcast.example.com", secret_key="Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
                        database_path=":memory:")
        missed = []
        with tempfile.TemporaryDirectory() as d:
            for name in SECRET_SETTINGS:
                path = os.path.join(d, name.lower())
                with open(path, "w") as f:
                    f.write("x" * 40)
                os.chmod(path, 0o644)
                by_id = {c.id: c for c in run_checks(config, redactor=Redactor(), environ={f"{name}_FILE": path})}
                if by_id["secret-files-protected"].status != "warn":
                    missed.append(name)
        self.assertEqual(missed, [])


class RevealFlagIsOffInProduction(unittest.TestCase):
    """The launcher sets up logging (INKCAST_LOG_LEVEL) before it loads the configuration, and load_config() reads
    every credential through a provider built with reveal_in_logs from INKCAST_LOG_REVEAL_SECRETS. In production the
    "value revealed" DEBUG lines were written first and validate() refused to start afterwards. A value shorter than
    the redactor's 8-character minimum (a Redis password, say) reached the log in clear."""

    def test_no_revealed_line_is_written_in_production(self):
        from inkcast.libs.config import ConfigError, load_config
        env = {"INKCAST_ENV": "production", "INKCAST_LOG_REVEAL_SECRETS": "true", "INKCAST_METRICS_PORT": "0",
               "INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
               "INKCAST_CACHE_URL": "redis://cache:6379/0", "INKCAST_CACHE_PASSWORD": "Rd7#kq2"}
        buf, _ = _json_lines("inkcast.libs.secrets", "DEBUG")
        self.addCleanup(buf.restore)
        config = load_config(env)
        with self.assertRaisesRegex(ConfigError, "INKCAST_LOG_REVEAL_SECRETS"):
            config.validate({"secret_key"})                # production still refuses the flag
        out = buf.getvalue()
        self.assertIn("Secret consumed", out)              # the masked DEBUG lines are there
        self.assertNotIn('"revealed": true', out)
        self.assertNotIn("Rd7#kq2", out)

    def test_the_flag_still_works_in_a_lab(self):
        from inkcast.libs.config import secrets_provider
        self.assertTrue(secrets_provider({"INKCAST_LOG_REVEAL_SECRETS": "true", "INKCAST_ENV": "development"})._reveal)


class ConfigFileErrorsShowNoContent(unittest.TestCase):
    """INKCAST_CONFIG_FILE must hold names only, and a broken .env line is reported by line number alone. A broken
    YAML file was reported with PyYAML's message, which quotes the offending line: the ConfigError went to the
    config.unsafe log event and stderr with whatever value sat on that line, a credential included."""

    def test_invalid_yaml_is_reported_by_position_not_content(self):
        import os
        import tempfile
        import traceback
        from inkcast.libs.config import ConfigError, settings_from
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inkcast.yaml")
            with open(path, "w") as f:
                f.write("INKCAST_PUBLIC_URL: https://inkcast.example.com\nINKCAST_SECRET_KEY: kq9-Xs2!vB7: tail\n")
            with self.assertRaises(ConfigError) as caught:
                settings_from({"INKCAST_CONFIG_FILE": path})
        rendered = "".join(traceback.format_exception(caught.exception))   # the message and any chained cause
        self.assertNotIn("kq9-Xs2", rendered)
        self.assertIn("not valid YAML", str(caught.exception))
        self.assertIn("line 2", str(caught.exception))


class UrlPasswordsWithoutAUser(unittest.TestCase):
    """The URL layer masks a password in any URL, registered or not (``postgres://u:[REDACTED]@host``). It needed a
    non-empty user name, so the usual Redis form ``redis://:password@host`` went to the log as it was, although
    the config-file guard (naming._URL_WITH_PASSWORD) already treats an empty user name as a password URL."""

    def test_an_empty_user_name_still_masks_the_password(self):
        from inkcast.libs.observability import redact
        from inkcast.libs.secrets import Redactor
        line = "cache error: redis://:Nq4-wz81LpRt@cache.inkcast.svc:6379/0 refused"
        self.assertNotIn("Nq4-wz81LpRt", Redactor().scrub(line))
        self.assertNotIn("Nq4-wz81LpRt", redact(line))
        self.assertEqual(Redactor().scrub("redis://:Nq4-wz81LpRt@cache:6379"), "redis://:[REDACTED]@cache:6379")
        self.assertEqual(Redactor().scrub("http://localhost:2773/x@y"), "http://localhost:2773/x@y")  # a port, no password


class JsonLinesMatchTheRegisteredForms(unittest.TestCase):
    """The redactor registers a value raw, URL-encoded and JSON-escaped with ``json.dumps``' default ASCII escapes;
    the JSON log formatter writes with ``ensure_ascii=False``. A value with a non-ASCII character and a character
    JSON escapes (a quote, a backslash) appeared in an ``extra=`` field or a traceback in a third form, which no
    registered variant matched."""

    def test_a_registered_value_is_masked_in_a_json_extra_field(self):
        from inkcast.libs.secrets import REDACTOR
        value = 'Grüße"Kennwort-4711'
        REDACTOR.register(value)
        buf, log = _json_lines("inkcast.review.json")
        self.addCleanup(buf.restore)
        log.warning("sign-in failed", extra={"detail": f"provider said {value}"})
        try:
            raise ValueError(f"rejected {value}")
        except ValueError:
            log.exception("and a traceback")
        out = buf.getvalue()
        self.assertNotIn("Kennwort-4711", out)
        self.assertEqual(out.count("[REDACTED]"), 2)   # the extra field and the traceback's last line


class SecretsEndpointErrorsAreScrubbed(unittest.TestCase):
    """status() (served as JSON on GET /secrets) carries each secret's last fetch error verbatim. The same text goes
    through the redactor on its way to the log, but not on its way to /secrets: a source whose error text quotes the
    value it holds (the "library's error text" case the redactor exists for) published it on the metrics port."""

    def test_last_error_on_the_secrets_endpoint_never_holds_a_value(self):
        import json
        from inkcast.libs.secrets import Fetched, Fetcher, Redactor, SecretsProvider

        class Flaky(Fetcher):
            kind = "flaky"
            fail = False

            def fetch(self, name, *, force=False):
                if self.fail:
                    raise RuntimeError("refused: stored value 'Vt9-c3QpLx77' no longer accepted")
                return Fetched("Vt9-c3QpLx77", "flaky", "v1")

        now = [0.0]
        fetcher, redactor = Flaky(), Redactor()
        provider = SecretsProvider(fetcher, ttl=10, clock=lambda: now[0], redactor=redactor)
        provider.get("OIDC_CLIENT_SECRET")
        fetcher.fail, now[0] = True, 20.0
        with self.assertLogs("inkcast.libs.secrets", "WARNING") as logged:
            self.assertEqual(provider.get("OIDC_CLIENT_SECRET").reveal(), "Vt9-c3QpLx77")   # served stale
        self.assertTrue(logged.records)
        [status] = provider.status()
        self.assertTrue(status["stale"])
        self.assertNotIn("Vt9-c3QpLx77", json.dumps(status))
        self.assertIn("RuntimeError", status["last_error"])


if __name__ == "__main__":
    unittest.main()
