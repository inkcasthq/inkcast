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


if __name__ == "__main__":
    unittest.main()
