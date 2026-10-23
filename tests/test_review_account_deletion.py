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
        erasure it calls for is done, or the person keeps every row until the reconcile (which erases a 404 at once
        only while Keycloak still holds the event, and otherwise after INKCAST_KEYCLOAK_ERASE_AFTER_DAYS, #86)."""
        import sqlite3
        import time
        from unittest import mock
        carol, dave = self.person("kc-rev-carol"), self.person("kc-rev-dave")
        self.assertEqual(self.sync_tick()["erased"], 0, "the cursor is set")
        self.fakes.kc_account_console_delete("kc-rev-carol")
        time.sleep(0.01)
        self.fakes.kc_account_console_delete("kc-rev-dave")                     # newer: Keycloak lists it first
        store, real, calls = self.app.store, self.app.store.erase_user, []

        def flaky(user_id, **as_read):
            calls.append(user_id)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(user_id, **as_read)
        with mock.patch.object(store, "erase_user", flaky):
            with self.assertRaises(sqlite3.OperationalError):
                self.sync_tick()
        self.sync_tick()
        for who in (carol, dave):
            self.assertEqual(set(self.rows(who["id"]).values()), {0}, f"{who['sub']} is erased on the next pass")
            self.assertEqual(self.statuses(who), [401, 401])

    def test_a_person_keycloak_still_has_is_not_told_their_deletion_is_being_finished(self):
        """Continue pressed, nothing confirmed at Keycloak (its "page has expired" and other error pages link Back to
        Application too). Keycloak answers that she exists, so no deletion is under way and none will finish: the page
        must say nothing was deleted, not that her data goes within a minute. The record stays, so confirming after
        all still ends in "deleted"."""
        import re
        ada = self.person("kc-rev-kept")
        link, before = self.link(ada), self.rows(ada["id"])

        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                page = await self.to_confirm_page(c, link)
                confirm = await self.continue_to_keycloak(c, page)
                early = await c.get(f"{self.base}/account/return")             # Keycloak still has her
                kept = self.rows(ada["id"])
                deleted = await c.post(self.keycloak_form(confirm), data={})    # she confirms after all
                back = re.search(r'href="([^"]+)"[^>]*>[^<]*Back to Application', deleted.text).group(1)
                return early, kept, await c.get(back)
        early, kept, home = asyncio.run(run())
        self.assertEqual(kept, before, "nothing erased while Keycloak has her")
        self.assertNotIn("being finished", early.text)
        self.assertNotIn("erases your data", early.text)
        self.assertIn("Nothing was deleted", early.text)
        self.assertIn("Your account is deleted", home.text, "the record was kept for the real return")
        self.assertEqual(set(self.rows(ada["id"]).values()), {0})

    def test_a_cookie_no_pending_deletion_matches_gets_the_neutral_page(self):
        """/account/return is Keycloak's Back to Application after an email verification or a password reset too. A
        cookie that matches no pending deletion (forged, planted from a sibling subdomain, left over) proves nothing:
        the page must be the neutral one, not "your account deletion is being finished"."""
        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                return await c.get(f"{self.base}/account/return", cookies={"inkcast_deletion": "x" * 43})
        r = asyncio.run(run())
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("delet", r.text.lower())
        self.assertIn("back at Inkcast", r.text)


class ReviewTokenLookups(unittest.TestCase):
    def test_a_token_lookup_reads_one_user_row_not_the_whole_users_table(self):
        """The PR's _ACTIVE_USER check runs on every OAuth token, code and PAT lookup, i.e. on every request. Written
        as ``user_id IN (SELECT id FROM users WHERE disabled_at IS NULL)``, SQLite lists every active user for each
        lookup (SCAN users: about 67 ms a lookup at 200,000 users, under the store's lock); the person's own row, by
        primary key, answers the same question."""
        from inkcast.storage import Store
        store = Store(":memory:", "k" * 40)
        ada = store.upsert_user("oidc:https://kc/realms/i#ada", "ada", None, None)["id"]
        store.save_code("code-1", "c1", ada, "{}")
        store.save_token("at-1", "access", "c1", ada, ["posts:read"], None, "f1", 3600)
        pat, _ = store.create_api_token(ada, "ci", ["posts:read"], None)
        seen: list[str] = []
        store._conn.set_trace_callback(seen.append)
        try:
            self.assertIsNotNone(store.get_code("code-1"))
            self.assertIsNotNone(store.get_token("at-1", "access"))
            self.assertIsNotNone(store.find_api_token(pat))
        finally:
            store._conn.set_trace_callback(None)
        lookups = [q for q in seen if q.lstrip().upper().startswith("SELECT") and "FROM users" in q]
        self.assertEqual(len(lookups), 3, seen)
        for q in lookups:
            plan = " | ".join(r["detail"] for r in store._all("EXPLAIN QUERY PLAN " + q))
            self.assertNotIn("SCAN users", plan, q)


del _AD

if __name__ == "__main__":
    unittest.main()
