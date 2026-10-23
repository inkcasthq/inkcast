# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""An account-deletion link is a one-time bearer secret: /account/delete/<ticket> starts that person's deletion flow.
The access log keeps paths, so the ticket must be masked there, as the connect ticket is (issue #149)."""

import io
import logging
import unittest

from starlette.testclient import TestClient

from test_review_identity import in_process, password_config


class NoDeletionLinksInLogs(unittest.TestCase):
    def test_the_access_log_never_holds_a_deletion_ticket(self):
        from inkcast.libs.observability import JsonFormatter, Redactor, TextFormatter
        cfg = password_config()
        app, ctx = in_process("IdentityService", cfg)
        user = ctx.store.upsert_user("local:owner", "owner", None, None)
        logger = logging.getLogger("inkcast.http")
        for formatter in (JsonFormatter("identity-service"), TextFormatter("%(message)s")):
            ticket = ctx.store.create_deletion_request(user["id"], ttl=900)
            out = io.StringIO()
            handler = logging.StreamHandler(out)
            handler.setFormatter(formatter)
            handler.addFilter(Redactor())
            logger.addHandler(handler)
            level, logger.level = logger.level, logging.INFO
            try:
                with TestClient(app, base_url=cfg.public_url, follow_redirects=False) as c:
                    c.get(f"/account/delete/{ticket}")
            finally:
                logger.removeHandler(handler)
                logger.setLevel(level)
            self.assertIn("GET /account/delete/", out.getvalue())                # the request is still logged
            self.assertNotIn(ticket, out.getvalue(), f"{type(formatter).__name__} wrote the ticket")


if __name__ == "__main__":
    unittest.main()
