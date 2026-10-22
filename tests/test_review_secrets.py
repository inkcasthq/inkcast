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


if __name__ == "__main__":
    unittest.main()
