# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of the DELETE_ACCOUNT look-back (issue #81, PR #85): each test failed on the PR's head."""

import asyncio
import unittest

import httpx2

from inkcast.libs.config import Config

ISSUER = "https://kc.example/realms/inkcast"


def _setup():
    from inkcast.libs.db import Store
    from inkcast.libs.secrets import SecretValue
    cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="oidc",
                 oidc_issuer=ISSUER, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                 oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync",
                 keycloak_sync_secret=SecretValue("sync-secret"))
    return cfg, Store(":memory:", "k" * 40)


def _keycloak(events: list[dict], gone: set[str]):
    """A Keycloak admin API: user events newest first (filtered by type and user, like RealmAdminResource.getEvents),
    no admin events, and 404 "User not found" for a person it no longer has."""
    def handler(request: httpx2.Request) -> httpx2.Response:
        path, q = request.url.path, request.url.params
        if path.endswith("/token"):
            return httpx2.Response(200, json={"access_token": "t", "expires_in": 300})
        if path.endswith("/admin-events"):
            return httpx2.Response(200, json=[])
        if path.endswith("/events"):
            found = [e for e in events if e["type"] in q.get_list("type") and q.get("user") in (None, e["userId"])]
            return httpx2.Response(200, json=sorted(found, key=lambda e: -e["time"])[: int(q.get("max", "100"))])
        keycloak_id = path.rsplit("/", 1)[1]
        if keycloak_id in gone:
            return httpx2.Response(404, json={"error": "User not found"})
        return httpx2.Response(200, json={"id": keycloak_id, "enabled": True})
    return handler


class ReSeenEventsSpareAnAccountKeycloakHasAgain(unittest.TestCase):
    """The look-back shows each DELETE_ACCOUNT event again on every pass until a newer one moves the cursor more than
    LOOKBACK_MS past it (the cursor only moves with DELETE_ACCOUNT events, so that can be weeks), and the PR relies on
    "an event seen again finds no user row". But Keycloak can have the person again under the same id, so the same
    identity key (oidc:<iss>#<sub>): an administrator restoring an account deleted by mistake from a realm export
    (partialImport keeps the id: UsersPartialImport.create, DefaultExportImportManager.createUser), or LDAP without
    import (the id is f:<component>:<username>). Once that person signs in again, the re-seen event erases the new
    account. On main the event, older than the cursor, was not read again."""

    def test_a_re_seen_delete_account_event_does_not_erase_an_account_keycloak_has_again(self):
        from inkcast.services.identity.keycloak_sync import KeycloakSync
        cfg, store = _setup()
        ada = store.upsert_user(f"oidc:{ISSUER}#kc-ada", "ada", None, None)
        store.upsert_user(f"oidc:{ISSUER}#kc-bob", "bob", None, None)
        now = 1_000_000.0
        events = [{"time": int(now * 1000) + 9_995, "type": "DELETE_ACCOUNT", "realmId": "inkcast", "userId": "kc-ada"},
                  {"time": int(now * 1000) + 10_000, "type": "DELETE_ACCOUNT", "realmId": "inkcast", "userId": "kc-bob"}]
        gone = {"kc-ada", "kc-bob"}

        async def two_passes():
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(_keycloak(events, gone))) as http:
                sync = KeycloakSync(cfg, store, http, clock=lambda: now)
                sync.last_reconcile = now                       # the reconcile isn't due: the events path alone
                first = await sync.tick()                       # both deleted their accounts: both erased
                gone.discard("kc-ada")                          # restored at Keycloak under the same id
                back = store.upsert_user(f"oidc:{ISSUER}#kc-ada", "ada", None, None)   # and she signs in again
                store.save_token("ic_at_" + "a" * 43, "access", "c1", back["id"], ["inkcast"], None, "f1", 3600)
                return first, back, await sync.tick()           # ada's event, 5 ms before the cursor, is seen again

        first, back, second = asyncio.run(two_passes())
        self.assertEqual(first["erased"], 2)
        self.assertIsNone(store.get_user(ada["id"]))
        self.assertEqual(second["erased"], 0, "an old DELETE_ACCOUNT event erased the account Keycloak has again")
        self.assertIsNotNone(store.get_user(back["id"]))
        self.assertIsNotNone(store.get_token("ic_at_" + "a" * 43, "access"))


class AnEventTheLookBackMissesIsStillErased(unittest.TestCase):
    """Issue #81: "The reconcile catches it later, but only if it erases people who no longer exist. Check that it
    does." It does not: it only cuts a 404 off (and never looks at a cut-off person again), so a DELETE_ACCOUNT event
    the pass misses leaves every row behind for good. A Keycloak replica whose clock runs two minutes fast moves the
    cursor past the events the others stamp in the meantime (by more than LOOKBACK_MS); so does a burst of more than
    500 between two passes. A person an administrator deleted (no DELETE_ACCOUNT event) is still only cut off."""

    def test_the_reconcile_erases_a_person_whose_delete_account_event_the_pass_missed(self):
        from inkcast.libs.db.erasure import user_hash
        from inkcast.services.identity.keycloak_sync import KeycloakSync
        cfg, store = _setup()
        bob, carol, dave = (store.upsert_user(f"oidc:{ISSUER}#kc-{n}", n, None, None) for n in ("bob", "carol", "dave"))
        store.save_token("ic_at_" + "c" * 43, "access", "c1", carol["id"], ["inkcast"], None, "f1", 3600)
        now = 1_000_000.0
        events = [{"time": int(now * 1000) + 120_000, "type": "DELETE_ACCOUNT", "realmId": "inkcast",
                   "userId": "kc-bob"}]                         # stamped by a replica two minutes fast
        gone = {"kc-bob"}

        async def two_passes():
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(_keycloak(events, gone))) as http:
                sync = KeycloakSync(cfg, store, http, clock=lambda: now)
                sync.last_reconcile = now
                first = await sync.tick()
                events.append({"time": int(now * 1000) + 5_000, "type": "DELETE_ACCOUNT", "realmId": "inkcast",
                               "userId": "kc-carol"})          # carol deletes hers on a replica with the right time
                gone.update({"kc-carol", "kc-dave"})            # and an administrator deletes dave
                sync.last_reconcile = 0                         # the reconcile is due
                return first, await sync.tick()

        first, second = asyncio.run(two_passes())
        self.assertEqual(first["erased"], 1)
        self.assertIsNone(store.get_user(bob["id"]))
        self.assertIsNone(store.get_user(carol["id"]), "carol deleted her account; the reconcile only cut her off")
        self.assertIsNone(store.get_token("ic_at_" + "c" * 43, "access"))
        self.assertEqual(second["erased"], 1)
        hashes = sorted(r["user_hash"] for r in store.user_erasures())
        self.assertEqual(hashes, sorted(user_hash(store._erasure_salt, who["id"]) for who in (bob, carol)))
        kept = store.get_user(dave["id"])
        self.assertIsNotNone(kept, "an administrator's deletion is cut off, not erased")
        self.assertIsNotNone(kept["disabled_at"])
        self.assertEqual(second["disabled"], 1)


if __name__ == "__main__":
    unittest.main()
