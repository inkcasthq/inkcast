# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #447: a failed sync pass is logged with the exception's type, and its message when it has one.

``KeycloakSync.run_forever`` logged ``"Keycloak user sync failed: %s", exc``. The event passes read Keycloak's event
lists through ``_get`` directly (``changed_people``, ``deleted_accounts``), so an httpx timeout there reaches the loop
as it is, and ``str()`` of an httpx timeout is empty: staging logged ``Keycloak user sync failed: `` with nothing to
diagnose. Both classes failed on main: the type was named nowhere, with or without a message."""

import asyncio
import unittest

import httpx2

from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.secrets import SecretValue
from inkcast.services.identity.keycloak_sync import KeycloakSync


def config() -> Config:
    return Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="oidc",
                  oidc_issuer="https://kc.example/realms/inkcast", oidc_client_id="inkcast",
                  oidc_client_secret=SecretValue("s"), oidc_required_role="inkcast-user",
                  keycloak_sync_client_id="inkcast-sync", keycloak_sync_secret=SecretValue("sync-secret"),
                  keycloak_sync_interval_seconds=0.01)


class FailedPass(unittest.TestCase):
    def run_until_one_failure(self, fail_events_once) -> list[str]:
        """Run the sync loop until its first failed pass, with ``fail_events_once(request)`` answering the first read of
        the user-events list; every other read answers an empty list. The ``identity.sync.failed`` lines logged."""
        store = Store(":memory:", "k" * 40)
        failed = asyncio.Event()
        reads: list[str] = []

        def keycloak(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/token"):
                return httpx2.Response(200, json={"access_token": "t", "expires_in": 300})
            if request.url.path.endswith("/events") and not reads:
                reads.append(request.url.path)
                return fail_events_once(request)
            return httpx2.Response(200, json=[])

        class Metrics:
            def inc(self, name, **labels):
                if labels.get("result") == "error":
                    failed.set()                 # the loop's wait returns at once on the stop below: no sleep

        async def run():
            stop = asyncio.Event()
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(keycloak)) as http:
                task = asyncio.create_task(KeycloakSync(config(), store, http, Metrics()).run_forever(stop))
                await asyncio.wait_for(failed.wait(), 5)
                stop.set()
                await asyncio.wait_for(task, 5)

        with self.assertLogs("inkcast.identity.sync", level="ERROR") as logged:
            asyncio.run(run())
        return [r.getMessage() for r in logged.records if getattr(r, "event", None) == "identity.sync.failed"]


class FindingAFailureWithoutAMessageNamesTheType(FailedPass):
    def test_an_httpx_timeout_reading_the_events_is_logged_by_its_type(self):
        def timed_out(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("", request=request)     # as httpx raises it: no message

        (line,) = self.run_until_one_failure(timed_out)
        self.assertIn("ReadTimeout", line, "the failed pass was logged with no reason")
        self.assertNotEqual(line.rstrip(), "Keycloak user sync failed:")


class FindingAFailureWithAMessageNamesTypeAndMessage(FailedPass):
    def test_a_sync_error_is_logged_with_its_type_and_its_message(self):
        (line,) = self.run_until_one_failure(lambda request: httpx2.Response(500, text="boom"))
        self.assertIn("reading events answered HTTP 500", line)
        self.assertIn("SyncError", line)


if __name__ == "__main__":
    unittest.main()
