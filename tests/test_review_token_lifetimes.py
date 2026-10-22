# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #132 (issue #90, F1.10.S2): token lifetimes as settings. Each test fails on the PR head."""

import unittest

from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config, PatPolicy
from inkcast.libs.http.app import compose

MAX = "INKCAST_PAT_MAX_DAYS"


def token_page(policy: PatPolicy):
    """(a client of IdentityService under this token policy, its store)."""
    cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="password",
                 password="p" * 12, pat=policy)
    specs = resolve("IdentityService")
    ctx = build(cfg, specs)
    return TestClient(compose(specs, ctx), base_url=cfg.public_url), ctx.store


class TokenPageOverTheCeiling(unittest.TestCase):
    """/tokens/new?days=N over 366. The acceptance and the README say every entry point refuses more than
    INKCAST_PAT_MAX_DAYS with a 422 naming the limit. The page built TokenCreate (le=366) before the policy check, so
    367 and up got a 400 "Check the token name, scopes and days." naming no limit: under production's own settings
    (INKCAST_PAT_MAX_DAYS=366) as under a lower maximum."""

    def test_more_than_366_days_is_a_422_naming_the_configured_maximum(self):
        for policy in (PatPolicy(90, 366, True), PatPolicy(45, 120, False)):
            for days in ("367", "400", "1000"):
                with self.subTest(max_days=policy.max_days, days=days):
                    c, store = token_page(policy)
                    self.addCleanup(store.close)
                    r = c.get("/tokens/new", params={"name": "ci", "scopes": "stories:sync", "days": days})
                    self.assertEqual(r.status_code, 422, r.text)
                    self.assertIn(f"at most {policy.max_days} days", r.text)
                    self.assertIn(f"{MAX}={policy.max_days}", r.text)
                    self.assertEqual(store._one("SELECT count(*) AS n FROM pending_auth")["n"], 0, "a sign-in started")


if __name__ == "__main__":
    unittest.main()
