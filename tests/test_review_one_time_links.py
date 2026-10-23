# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #147 (issue #92, F1.10.S4): the one-time link lifetime as a setting.

A deletion link is "valid N minutes, one use": opening it is the use. Once opened it is spent (``link_hash`` is
NULL) and the flow it starts is bound to the browser that signs in and to the person who asked for it, so what
follows (the fresh sign-in, reading the confirm page, Continue) must not be cut short by the link's own lifetime
running out behind it."""

import asyncio
import re
import time
import types
import unittest
from unittest import mock
from urllib.parse import urljoin

import httpx2

from inkcast.libs.account import deletion_link
from test_e2e import Harness

HOUR = 3600


class ADeletionLinkOpenedInTime(Harness):
    """Under a 60-minute setting, a link opened before its hour is up leads to the confirm page and on to Keycloak,
    however late in the hour it was opened: the deletion request lives on for the sign-in it starts."""

    extra_config = {"keycloak_sync_client_id": "inkcast-sync", "keycloak_sync_secret": "sync-secret",
                    "one_time_link_minutes": 60}

    def setUp(self):
        self.now = time.time()
        stopped = types.SimpleNamespace(time=lambda: self.now)   # the identity repository's clock, moved by hand
        patcher = mock.patch("inkcast.libs.db.identity.time", stopped)
        patcher.start()
        self.addCleanup(patcher.stop)
        first = dict(self.fakes.next_user)
        self.addCleanup(setattr, self.fakes, "next_user", first)

    def link(self, sub: str) -> tuple[str, float]:
        """(the deletion link, when it was made) for a person the fake Keycloak signs in as ``sub``."""
        self.fakes.next_user = {**self.fakes.next_user, "sub": sub, "preferred_username": sub, "name": "Account Owner",
                                "email": f"{sub}@example.com"}
        user = self.app.store.upsert_user(f"oidc:{self.fakes.issuer}#{sub}", sub, f"{sub}@example.com", None)
        made = self.now
        link = deletion_link(self.config, self.app.store, user["id"])
        self.assertEqual(link.expires_in, HOUR)
        return link.url, made

    async def open_and_sign_in(self, c: httpx2.AsyncClient, url: str, opened: float, back: float) -> httpx2.Response:
        """Open the link at ``opened``; the fresh sign-in at Keycloak brings the browser back at ``back``."""
        self.now = opened
        r = await c.get(url)
        self.assertIn(r.status_code, (302, 303), r.text[:300])    # the link opened: off to sign in again
        self.now = back
        for _ in range(8):
            if r.status_code not in (302, 303) or "/auth/consent" in r.headers["location"]:
                break
            r = await c.get(urljoin(str(r.url), r.headers["location"]))
        self.assertIn("/auth/consent", r.headers.get("location", ""), r.text[:300])
        return await c.get(urljoin(str(r.url), r.headers["location"]))

    def test_a_link_opened_in_its_last_seconds_still_reaches_the_confirm_page(self):
        url, made = self.link("kc-review-late-open")

        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                return await self.open_and_sign_in(c, url, opened=made + HOUR - 10, back=made + HOUR + 20)
        page = asyncio.run(run())
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("Delete your Inkcast account?", page.text)
        self.assertNotIn("belongs to another account", page.text)

    def test_a_link_opened_at_minute_50_can_still_continue_at_minute_61(self):
        url, made = self.link("kc-review-slow-read")

        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                page = await self.open_and_sign_in(c, url, opened=made + 50 * 60, back=made + 50 * 60 + 30)
                self.assertEqual(page.status_code, 200, page.text[:300])
                fields = dict(re.findall(r'name=(\w+) value="([^"]*)"', page.text))
                self.now = made + 61 * 60                          # eleven minutes reading what goes and what stays
                return await c.post(f"{self.config.public_url}/auth/consent",
                                    data={"pending": fields["pending"], "csrf": fields["csrf"], "decision": "approve"})
        r = asyncio.run(run())
        self.assertEqual(r.status_code, 302, r.text[:300])
        self.assertIn("kc_action=delete_account", r.headers["location"])


if __name__ == "__main__":
    unittest.main()
