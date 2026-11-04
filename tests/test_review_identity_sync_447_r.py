# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #450 (issue #447): the failed-pass line names the exception, and never carries a token.

``changed_people`` and ``deleted_accounts`` read Keycloak through ``_get`` as they are, so an httpx2 error there reaches
``run_forever`` unwrapped, and #450 logs its text beside its type. httpx2's ``LocalProtocolError`` quotes the header it
refused: an access token Keycloak answers with a CR, an LF or a NUL in it is refused by h11 when ``_get`` sends it as
``Authorization: Bearer <token>``, and the error reads ``Illegal header value b'Bearer <token>'``. The log handler's
``Redactor`` masks a run of token characters after "Bearer ", so what reached stdout was the token past its first
control character. ``_access_token`` and ``_read_person`` already keep only an httpx2 error's class name.

#450 also renders the exception's text itself, in the ``except`` block, where main left ``%s`` to logging, which handles a
text that can't be rendered (``Handler.handleError``). An exception whose ``str()`` raises (the pipeline's #174 names the
case: "an error whose text can't be rendered") then escaped ``run_forever``: the sync loop ended at its first failed pass,
and nobody was disabled or erased again until a restart. On main the loop went on."""

import asyncio
import io
import logging
import unittest

import httpx2

from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.observability import JsonFormatter, Redactor
from inkcast.libs.secrets import SecretValue
from inkcast.services.identity.keycloak_sync import KeycloakSync

TAIL = "c2lnbmF0dXJlLW9mLXRoZS10b2tlbg"          # the token past its control character: what the Redactor let through
TOKEN = f"eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeW5jIn0\r\n.{TAIL}"


def config(issuer: str) -> Config:
    return Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="oidc",
                  oidc_issuer=issuer, oidc_client_id="inkcast", oidc_client_secret=SecretValue("s"),
                  oidc_required_role="inkcast-user", keycloak_sync_client_id="inkcast-sync",
                  keycloak_sync_secret=SecretValue("sync-secret"), keycloak_sync_interval_seconds=0.01)


class SyncCase(unittest.TestCase):
    def store(self) -> Store:
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        return store


class FindingAFailedPassNeverLogsTheBearerToken(SyncCase):
    def test_a_header_h11_refuses_is_logged_by_its_type_alone(self):
        rendered = io.StringIO()
        handler = logging.StreamHandler(rendered)                           # as setup_logging("json") renders a line
        handler.setFormatter(JsonFormatter())
        handler.addFilter(Redactor())
        logger = logging.getLogger("inkcast.identity.sync")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.propagate = False                                            # this handler only: assertLogs would replace it
        self.addCleanup(setattr, logger, "propagate", True)

        async def run():
            listener = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
            port = listener.sockets[0].getsockname()[1]
            real = httpx2.AsyncHTTPTransport()                              # h11 checks the headers, as in production

            class Keycloak(httpx2.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    if request.url.path.endswith("/token"):
                        return httpx2.Response(200, json={"access_token": TOKEN, "expires_in": 300})
                    return await real.handle_async_request(request)

            failed = asyncio.Event()

            class Metrics:
                def inc(self, name, **labels):
                    if labels.get("result") == "error":
                        failed.set()

            stop = asyncio.Event()
            async with httpx2.AsyncClient(transport=Keycloak()) as http:
                sync = KeycloakSync(config(f"http://127.0.0.1:{port}/realms/inkcast"), self.store(), http, Metrics())
                task = asyncio.create_task(sync.run_forever(stop))
                await asyncio.wait_for(failed.wait(), 5)
                stop.set()
                await asyncio.wait_for(task, 5)
            await real.aclose()
            listener.close()
            await listener.wait_closed()

        asyncio.run(run())
        (line,) = [text for text in rendered.getvalue().splitlines() if '"identity.sync.failed"' in text]
        self.assertIn("LocalProtocolError", line, "the type is not named")
        self.assertNotIn(TAIL, line, "the access token reached the log")
        self.assertNotIn("Illegal header value", line)


class Unrenderable(Exception):
    """An exception whose text can't be rendered: an attribute its ``__str__`` reads was never set."""

    def __str__(self):
        return f"{self.detail}"


class FindingAnExceptionWhoseTextCannotBeRenderedDoesNotEndTheLoop(SyncCase):
    def test_the_failed_pass_is_logged_by_its_type_and_the_next_pass_runs(self):
        passes = []

        async def tick():
            passes.append(1)
            raise Unrenderable()

        async def run():
            stop = asyncio.Event()
            async with httpx2.AsyncClient() as http:
                sync = KeycloakSync(config("https://kc.example/realms/inkcast"), self.store(), http)
                sync.tick = tick
                task = asyncio.create_task(sync.run_forever(stop))
                while len(passes) < 2 and not task.done():
                    await asyncio.sleep(0.01)
                stop.set()
                await asyncio.wait_for(task, 5)                             # raised AttributeError on #450's head

        with self.assertLogs("inkcast.identity.sync", level="ERROR") as logged:
            asyncio.run(run())
        self.assertGreaterEqual(len(passes), 2, "the loop ended at its first failed pass")
        failed = [r.getMessage() for r in logged.records if getattr(r, "event", None) == "identity.sync.failed"]
        self.assertIn("Keycloak user sync failed: Unrenderable", failed)


if __name__ == "__main__":
    unittest.main()
