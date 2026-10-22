# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of Delete my account (E9.F9.5.S4, issue #41, PR #69): each test failed on the PR's head."""

import asyncio
import unittest

import httpx2

import test_e2e
from test_e2e import Harness

_AD = test_e2e.AccountDeletion      # its helpers, not its tests (the name is deleted below, so not collected here)


class ReviewAccountDeletion(Harness):
    extra_config = _AD.extra_config
    person, call, statuses, rows, link = _AD.person, _AD.call, _AD.statuses, _AD.rows, _AD.link
    to_confirm_page, continue_to_keycloak = _AD.to_confirm_page, _AD.continue_to_keycloak
    keycloak_form = staticmethod(_AD.keycloak_form)
    sync_tick, async_sync_tick = _AD.sync_tick, _AD.async_sync_tick
    setUp = _AD.setUp

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fakes.kc_base_url = f"{cls.config.public_url}/account/return"

    def test_a_404_that_is_not_keycloaks_user_not_found_erases_nothing(self):
        """Keycloak's reverse-proxy guide says not to expose /admin/ on the public host, and the sync derives its
        admin URL from the public issuer: there every admin path answers a plain 404. A person who pressed Continue
        but never confirmed at Keycloak, then comes back to /account/return, must not be erased on that 404."""
        from inkcast.services.identity.keycloak_sync import KeycloakSync
        ada = self.person("kc-rev-proxy")
        link, before = self.link(ada), self.rows(ada["id"])
        hidden = KeycloakSync(self.config, self.app.store, self.app.http, self.app.metrics)
        hidden.admin = f"{self.fakes.base}/not-exposed/admin/realms/inkcast"   # what the proxy answers: 404 Not Found
        previous = self.app.extras.get("keycloak_sync")
        self.app.extras["keycloak_sync"] = hidden
        self.addCleanup(lambda: self.app.extras.pop("keycloak_sync") if previous is None
                        else self.app.extras.__setitem__("keycloak_sync", previous))

        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                page = await self.to_confirm_page(c, link)
                await self.continue_to_keycloak(c, page)                       # armed; she does not confirm
                return await c.get(f"{self.base}/account/return")
        back = asyncio.run(run())
        self.assertNotIn("kc-rev-proxy", self.fakes.kc_deleted, "Keycloak still has her")
        self.assertEqual(self.rows(ada["id"]), before, "nothing erased on a 404 that is not Keycloak's 'User not found'")
        self.assertEqual(self.statuses(ada), [200, 200])
        self.assertNotIn("Your account is deleted", back.text)

    def test_a_failed_erasure_in_the_event_pass_is_read_again(self):
        """Two people delete their accounts from the account console; the database refuses the first erasure (locked,
        a failover). The next pass must erase both: the DELETE_ACCOUNT cursor may only move past an event once the
        erasure it calls for is done, or the person keeps every row (the reconcile only disables a 404)."""
        import sqlite3
        import time
        from unittest import mock
        carol, dave = self.person("kc-rev-carol"), self.person("kc-rev-dave")
        self.assertEqual(self.sync_tick()["erased"], 0, "the cursor is set")
        self.fakes.kc_account_console_delete("kc-rev-carol")
        time.sleep(0.01)
        self.fakes.kc_account_console_delete("kc-rev-dave")                     # newer: Keycloak lists it first
        store, real, calls = self.app.store, self.app.store.erase_user, []

        def flaky(user_id):
            calls.append(user_id)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(user_id)
        with mock.patch.object(store, "erase_user", flaky):
            with self.assertRaises(sqlite3.OperationalError):
                self.sync_tick()
        self.sync_tick()
        for who in (carol, dave):
            self.assertEqual(set(self.rows(who["id"]).values()), {0}, f"{who['sub']} is erased on the next pass")
            self.assertEqual(self.statuses(who), [401, 401])


del _AD

if __name__ == "__main__":
    unittest.main()
