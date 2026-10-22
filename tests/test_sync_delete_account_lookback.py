# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #81: a DELETE_ACCOUNT user event that becomes visible late must still erase the person, exactly once.

Keycloak stamps an event when the request starts and stores it when it commits, and replicas have their own clocks,
so events don't become visible in time order. ``changed_people`` re-reads ``LOOKBACK_MS`` before its cursor for that
reason (#75); ``deleted_accounts`` read ``at >= cursor`` with no look-back, so a DELETE_ACCOUNT event stamped a
little before the newest one already seen was skipped. The look-back then shows the same event again on every pass
for a minute: re-seeing an erased person must erase nothing, count nothing and write no second audit record."""

import asyncio
import unittest

import httpx2

from inkcast.libs.config import Config


class DeleteAccountEventsSeenLate(unittest.TestCase):
    def test_a_delete_account_event_that_shows_up_after_a_newer_one_still_erases_the_person_once(self):
        from inkcast.libs.db import Store
        from inkcast.libs.db.erasure import user_hash
        from inkcast.libs.secrets import SecretValue
        from inkcast.services.identity.keycloak_sync import KeycloakSync
        issuer = "https://kc.example/realms/inkcast"
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="oidc",
                     oidc_issuer=issuer, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                     oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync",
                     keycloak_sync_secret=SecretValue("sync-secret"))
        store = Store(":memory:", "k" * 40)
        ada = store.upsert_user(f"oidc:{issuer}#kc-ada", "ada", None, None)
        bob = store.upsert_user(f"oidc:{issuer}#kc-bob", "bob", None, None)
        store.save_token("ic_at_" + "a" * 43, "access", "c1", ada["id"], ["inkcast"], None, "f1", 3600)
        store.save_token("ic_at_" + "b" * 43, "access", "c1", bob["id"], ["inkcast"], None, "f2", 3600)
        now = 1_000_000.0
        bob_at, ada_at = int(now * 1000) + 10_000, int(now * 1000) + 9_995          # ada's, 5 ms earlier
        events = [{"time": bob_at, "type": "DELETE_ACCOUNT", "realmId": "inkcast", "userId": "kc-bob"}]
        counted: list[tuple[str, tuple[tuple[str, str], ...]]] = []

        class Metrics:
            def inc(self, name, amount=1, **labels):
                counted.append((name, tuple(sorted(labels.items()))))

        def keycloak(request: httpx2.Request) -> httpx2.Response:
            path = request.url.path
            if path.endswith("/token"):
                return httpx2.Response(200, json={"access_token": "t", "expires_in": 300})
            if path.endswith("/admin-events"):
                return httpx2.Response(200, json=[])
            if path.endswith("/events"):
                return httpx2.Response(200, json=sorted(events, key=lambda e: -e["time"]))   # newest first
            if path.endswith(("/users/kc-ada", "/users/kc-bob")):                           # both deleted theirs
                return httpx2.Response(404, json={"error": "User not found"})
            return httpx2.Response(200, json={"enabled": True})

        async def three_passes():
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(keycloak)) as http:
                sync = KeycloakSync(cfg, store, http, Metrics(), clock=lambda: now)
                sync.last_reconcile = now                       # the reconcile isn't due: the events path alone
                first = await sync.tick()                       # sees bob's event; ada's isn't committed yet
                events.append({"time": ada_at, "type": "DELETE_ACCOUNT", "realmId": "inkcast", "userId": "kc-ada"})
                second = await sync.tick()
                return first, second, await sync.tick()         # both events are still visible, inside the look-back

        first, second, third = asyncio.run(three_passes())
        self.assertEqual(first["erased"], 1)
        self.assertEqual(second["erased"], 1, "ada's DELETE_ACCOUNT event turned up late and was skipped")
        self.assertIsNone(store.get_token("ic_at_" + "a" * 43, "access"))
        self.assertIsNone(store.get_user(ada["id"]))
        self.assertIsNone(store.find_user_by_identity(f"oidc:{issuer}#kc-ada"))

        # Exactly once each, however often the look-back shows the events again.
        self.assertEqual(third["erased"], 0, "a re-seen event erased again")
        deleted = [labels for name, labels in counted if name == "inkcast_identity_users_deleted_total"]
        self.assertEqual(deleted, [(("via", "event"),)] * 2, "one increment per person, not per sighting")
        hashes = [record["user_hash"] for record in store.user_erasures()]
        self.assertEqual(sorted(hashes), sorted(user_hash(store._erasure_salt, who["id"]) for who in (ada, bob)))


if __name__ == "__main__":
    unittest.main()
