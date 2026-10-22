# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #98 (issue #86): a person gone from Keycloak is erased after
INKCAST_KEYCLOAK_ERASE_AFTER_DAYS.

The erasure is irreversible, so the reconcile may only act on an answer about this very person that is still true
when it acts. Each test is a way a person Keycloak still has, or one deleted again a moment ago, loses every row."""

import asyncio
import unittest
from urllib.parse import unquote

import httpx2

from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.secrets import SecretValue
from inkcast.services.identity.keycloak_sync import KeycloakSync

ISSUER = "https://kc.example/realms/inkcast"
DAY = 86400
T0 = 1_700_000_000.0


class Keycloak:
    """The admin API as Keycloak 26.6 answers it: ``users/{id}`` is 404 ``User not found`` for any id it holds no
    user under (UsersResource.user), else 200 with ``enabled``; the id is the path segment as sent, decoded the way
    JAX-RS decodes a path parameter. ``during_user_events`` runs while a DELETE_ACCOUNT query about one person (the
    reconcile's ``deleted_own_account``) is in flight."""

    def __init__(self):
        self.users: dict[str, bool] = {}            # Keycloak id -> enabled
        self.admin_events: list[dict] = []
        self.during_user_events = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.raw_path.decode().split("?", 1)[0]
        if path.endswith("/token"):
            return httpx2.Response(200, json={"access_token": "t", "expires_in": 300})
        if path.endswith("/admin-events"):
            return httpx2.Response(200, json=sorted(self.admin_events, key=lambda e: -e["time"]))
        if path.endswith("/events"):
            if request.url.params.get("user") and self.during_user_events:
                hook, self.during_user_events = self.during_user_events, None
                hook()
            return httpx2.Response(200, json=[])
        keycloak_id = unquote(path.rsplit("/", 1)[1])
        if keycloak_id not in self.users:
            return httpx2.Response(404, json={"error": "User not found"})
        return httpx2.Response(200, json={"id": keycloak_id, "enabled": self.users[keycloak_id]})


class Review(unittest.TestCase):
    def setUp(self):
        self.kc = Keycloak()
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.now = T0
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.kc))
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))

    def person(self, keycloak_id: str) -> dict:
        self.kc.users[keycloak_id] = True
        return self.store.upsert_user(f"oidc:{ISSUER}#{keycloak_id}", keycloak_id, None, None)

    def sync(self, days: "int | None") -> KeycloakSync:
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="oidc",
                     oidc_issuer=ISSUER, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                     oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync",
                     keycloak_sync_secret=SecretValue("sync-secret"), keycloak_erase_after_days=days)
        cfg.validate()
        worker = KeycloakSync(cfg, self.store, self.http, clock=lambda: self.now)
        worker.last_reconcile = self.now
        return worker

    def tick(self, worker: KeycloakSync, at: "float | None" = None) -> dict:
        if at is not None:
            self.now = at
        return asyncio.run(worker.tick())

    def reconcile(self, worker: KeycloakSync, at: "float | None" = None) -> dict:
        worker.last_reconcile = 0
        return self.tick(worker, at)


class AnIdTheUrlCutsShort(Review):
    """``person()`` puts the Keycloak id into the URL as it is. Keycloak makes UUIDs, but a realm import keeps the id
    the export carried (DefaultExportImportManager.createUser, 26.6.1), and a user-storage provider's ids are
    ``f:<component>:<its own id>``. An id with ``#`` or ``?`` is cut short by the URL: the sync asks about
    ``kc-ada``, Keycloak truthfully answers that it has no such user, and the person it does have is erased."""

    def test_a_present_person_whose_id_has_a_reserved_character_is_never_erased(self):
        for keycloak_id in ("kc-ada#2", "kc-ada?2"):
            with self.subTest(keycloak_id=keycloak_id):
                self.setUp()
                ada = self.person(keycloak_id)
                worker = self.sync(30)
                self.reconcile(worker)
                self.reconcile(worker, T0 + 30 * DAY)
                row = self.store.get_user(ada["id"])
                self.assertIsNotNone(row, "Keycloak has her, enabled: never erased")
                self.assertIsNone(row["disabled_at"], "nor cut off")


class ASignInWhileTheReconcileAsks(Review):
    """The reconcile reads a 404, then awaits Keycloak again (``deleted_own_account``) before it acts; the sign-in
    callback runs in the same process meanwhile. With ``0`` the PR erases a person who signed in through Keycloak
    after that 404 was read, the one proof Inkcast has that Keycloak has them. With ``N > 0`` the sign-in clears
    ``deleted_at`` and ``_cut_off`` starts it again, so only ``0`` acts on the stale answer."""

    def test_zero_does_not_erase_a_person_who_signed_in_after_the_404_was_read(self):
        ada = self.person("kc-ada")
        self.store._exec("UPDATE users SET last_login_at = ? WHERE id = ?", T0 - DAY, ada["id"])
        worker = self.sync(0)
        del self.kc.users["kc-ada"]                                 # deleted by mistake: the reconcile reads the 404

        def restored_and_signed_in():                               # ...and she is back and signs in, mid-check
            self.kc.users["kc-ada"] = True
            self.store.upsert_user(f"oidc:{ISSUER}#kc-ada", "kc-ada", None, None)

        self.kc.during_user_events = restored_and_signed_in
        done = self.reconcile(worker)
        self.assertIsNotNone(self.store.get_user(ada["id"]), "she signed in after the 404: not erased on it")
        self.assertEqual(done["erased"], 0)
        self.assertEqual(self.store.user_erasures(), [])


if __name__ == "__main__":
    unittest.main()
