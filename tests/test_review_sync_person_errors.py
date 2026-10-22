# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #115 (issue #103): one person's failing Keycloak lookup no longer blocks everyone after
them. Each test failed on the PR's head."""

import asyncio
import unittest

import httpx2

from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.secrets import SecretValue
from inkcast.services.identity.keycloak_sync import KeycloakSync, SyncError

ISSUER = "https://kc.example/realms/inkcast"
T0 = 1_700_000_000.0


class ARealmWideRefusalIsNotAStorm(unittest.TestCase):
    """The service account lost ``view-users`` (it keeps ``view-events``): every ``users/{id}`` answers 403 and the
    event lists still answer. The PR fails such a reconcile as a whole ("could act on nobody") and leaves
    ``last_reconcile`` alone, so the reconcile is due again at the next pass, and it now reads every person before
    it can tell. On main the first 403 ended the pass: one read per pass. With the PR every pass (30 s) reads all N
    Keycloak-backed people and logs and counts N failures, for as long as the outage lasts: for 10,000 people that is
    10,000 admin API calls every 30 s against a Keycloak that is already failing. The pass must still fail at every
    tick (``InkcastIdentitySyncFailing`` keeps its meaning), but everyone is read again on the reconcile's schedule."""

    def test_a_reconcile_that_could_act_on_nobody_reads_everyone_again_on_schedule_not_every_pass(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        people = [store.upsert_user(f"oidc:{ISSUER}#kc-{n:02d}", f"p{n}", None, None) for n in range(20)]
        reads: list[str] = []
        refused = {"on": True}

        def keycloak(request: httpx2.Request) -> httpx2.Response:
            path = request.url.path
            if path.endswith("/token"):
                return httpx2.Response(200, json={"access_token": "t", "expires_in": 300})
            if path.endswith(("/admin-events", "/events")):
                return httpx2.Response(200, json=[])
            reads.append(path.rsplit("/", 1)[1])
            if refused["on"]:
                return httpx2.Response(403, json={"error": "HTTP 403 Forbidden"})
            return httpx2.Response(200, json={"enabled": True})

        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="oidc",
                     oidc_issuer=ISSUER, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                     oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync",
                     keycloak_sync_secret=SecretValue("sync-secret"), keycloak_reconcile_seconds=600.0)
        cfg.validate()
        now = {"t": T0}
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(keycloak))
        self.addCleanup(lambda: asyncio.run(http.aclose()))
        worker = KeycloakSync(cfg, store, http, clock=lambda: now["t"])     # at start: the reconcile is due

        def tick(at: float) -> "dict | Exception":
            now["t"] = at
            try:
                return asyncio.run(worker.tick())
            except Exception as exc:
                return exc

        first = tick(T0)
        self.assertIs(type(first), SyncError, "a reconcile that could act on nobody fails the pass (the PR's rule)")
        self.assertEqual(len(reads), len(people))

        reads.clear()
        between = [tick(T0 + seconds) for seconds in range(30, 600, 30)]    # 19 passes before the next is due
        self.assertTrue(all(isinstance(r, SyncError) for r in between),
                        "every pass still fails while nobody can be read: InkcastIdentitySyncFailing keeps its meaning")
        self.assertEqual(len(reads), 0, f"{len(reads)} users/{{id}} reads in 19 passes: the whole realm re-read every "
                                        "30 s (main read one person per failed pass)")

        self.assertIs(type(tick(T0 + 600)), SyncError, "due again: everyone is read, still refused")
        self.assertEqual(len(reads), len(people))

        refused["on"] = False                                                # view-users granted again
        reads.clear()
        self.assertIsInstance(tick(T0 + 1200), dict, "the next scheduled reconcile reads everyone and succeeds")
        self.assertEqual(len(reads), len(people))
        self.assertEqual(worker.last_reconcile, T0 + 1200)
        self.assertIsInstance(tick(T0 + 1230), dict, "and the passes after it are the events path alone")
        self.assertEqual(len(reads), len(people))


if __name__ == "__main__":
    unittest.main()
