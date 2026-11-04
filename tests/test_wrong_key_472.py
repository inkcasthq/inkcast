# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #472: a scheduler on the wrong INKCAST_SECRET_KEY, from the reviews of #451 (#446) and #454 (#445).

A resend of a post whose credentials were sealed under another key answered 500: ``Publisher.resend`` read them with
``store.get_credentials``, and ``Sealer.unseal`` raised ``RuntimeError: Stored credentials can't be decrypted``. It is
refused now, before anything is cancelled, as a disconnected platform's is: 409 ``not_connected``, naming the
reconnect, through the use case, REST and MCP alike.

Each test failed on main (d70d55a6)."""

import asyncio
import tempfile
import time
import unittest

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from inkcast.launcher import build, resolve
from inkcast.libs.db import Store
from inkcast.libs.http.app import compose
from inkcast.libs.observability import EVENTS
from inkcast.libs.platforms import Prepared
from inkcast.services.publishing.usecases import ServiceError

from test_e2e import free_port, serve
from test_one_time_link_setting import by_hand, oauth_token
from test_person_decides import Harness
from test_person_decides_followups import Refusing
from test_review_person_decides_234_r import Disconnecting

OTHER_KEY = "j" * 40                                  # the key the credentials were sealed with; the stores run on k * 40


# ---- a resend of credentials sealed under another key is refused ----------------------------------------------------


class FindingAResendOfUnreadableCredentialsIsRefusedBeforeAnythingIsCancelled(Harness):
    platform_class = Refusing

    def seal_elsewhere(self) -> None:
        other = Store(self.path, OTHER_KEY)
        self.addCleanup(other.close)
        other.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})

    def test_the_resend_answers_409_naming_the_reconnect_and_the_post_stays_unconfirmed(self):
        """The resend raised ``RuntimeError: Stored credentials can't be decrypted`` out of the use case (a 500 over
        REST), the post the cap's #451 leaves for a person to resend."""
        old_id = self.unconfirmed()
        self.seal_elsewhere()
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING") as logged:
            with self.assertRaises(ServiceError) as caught:                   # RuntimeError on main
                asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_connected"))
        message = caught.exception.message
        self.assertIn("Findable's stored credentials can't be decrypted", message)
        self.assertIn("INKCAST_SECRET_KEY", message)
        self.assertIn(f"post {old_id} was not resent and is still unconfirmed", message)
        self.assertIn("Reconnect Findable (inkcast_connect, or POST /api/v1/platforms/findable/connect-link), then resend "
                      f"post {old_id}.", message)
        self.assertEqual((self.status(old_id), len(self.posts()), self.platform.sent, self.platform.verified),
                         ("unconfirmed", 1, [], 0), "something was cancelled, made or sent")
        self.assertEqual([t for t, _, _ in self.events(old_id)], [])
        refused = [r for r in logged.records if getattr(r, "event", None) == "publish.resend.unreadable"]
        self.assertEqual([(r.post_id, r.platform) for r in refused], [(old_id, "findable")], "the refusal is not logged")
        self.assertIn("publish.resend.unreadable", EVENTS)

        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
        info = asyncio.run(self.service.resend(self.user, old_id))           # reconnected: the decision goes through
        self.assertEqual((info.status, self.status(old_id), self.platform.sent), ("published", "cancelled", ["t"]))


class FindingCredentialsResealedWhileTheCheckIsOutRefuseTheResend(Harness):
    platform_class = Disconnecting

    def test_credentials_sealed_under_another_key_while_verify_is_out_refuse_it_before_the_cancel(self):
        """The credentials are read twice: before ``verify`` and after it. A pod on another key that stores them while
        the check is out (a reconnect through it, during a rotation) made the second read raise, the same 500."""
        old_id = self.unconfirmed()
        other = Store(self.path, OTHER_KEY)
        self.addCleanup(other.close)
        self.platform.during_verify = lambda: other.save_credentials(self.user, self.platform.id, {"k": "v"},
                                                                     {"id": "1", "name": "findable"})
        with self.assertRaises(ServiceError) as caught:                       # RuntimeError on main
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_connected"))
        self.assertEqual((self.status(old_id), len(self.posts()), self.platform.sent), ("unconfirmed", 1, []))


class FindingTheUnreadableCredentialsErrorHasAName(unittest.TestCase):
    def test_the_named_error_is_raised_by_the_store_and_every_guard_that_caught_a_runtime_error_still_does(self):
        """``Sealer.unseal`` raised a bare RuntimeError, which a caller could tell apart only by its text. It is still
        a RuntimeError, so the scheduler's guards (#446) catch it as before."""
        from inkcast.libs.secrets import CredentialsUnreadable

        path = tempfile.mkdtemp() + "/inkcast.db"
        sealed, reading = Store(path, OTHER_KEY), Store(path, "k" * 40)
        self.addCleanup(sealed.close)
        self.addCleanup(reading.close)
        user = reading.upsert_user("local:u", "u", None, None)["id"]
        sealed.save_credentials(user, "findable", {"k": "v"}, {"id": "1", "name": "findable"})
        with self.assertRaises(CredentialsUnreadable) as caught:
            reading.get_credentials(user, "findable")
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertIn("INKCAST_SECRET_KEY changed", str(caught.exception))


class FindingRestAndMcpAnswerTheRefusal(unittest.TestCase):
    """Over real HTTP, signed in with an OAuth token: both front doors of the resend, on the real Medium plug-in (the
    credentials are read before anything goes to the platform, so nothing leaves this process)."""

    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.path = tempfile.mkdtemp() + "/inkcast.db"
        cls.cfg = by_hand(public_url=f"http://127.0.0.1:{port}", port=port, secret_key="k" * 48, database_path=cls.path)
        cls.cfg.validate()
        specs = resolve("PublishingApi")
        cls.ctx = build(cls.cfg, specs)
        cls.server = serve(compose(specs, cls.ctx), port)
        cls.access, cls.user = oauth_token(cls.ctx.store)
        other = Store(cls.path, OTHER_KEY)
        other.save_credentials(cls.user, "medium", {"token": "t"}, {"id": "1", "name": "medium"})
        other.close()

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.ctx.store.close()

    def unconfirmed(self) -> str:
        prepared = Prepared(platform="medium", title="t", payload={"body": "Body."}, preview={})
        return self.ctx.store.create_post(group_id="g", user_id=self.user, platform="medium", title="t",
                                          status="unconfirmed", run_at=time.time() - 1, payload=prepared.to_dict())

    def test_rest_answers_409_not_connected_and_the_post_stays_unconfirmed(self):
        """REST answered 500 Internal Server Error."""
        post_id = self.unconfirmed()
        r = httpx2.post(f"{self.cfg.public_url}/api/v1/posts/{post_id}/resend",
                        headers={"Authorization": f"Bearer {self.access}"}, timeout=30)
        self.assertEqual(r.status_code, 409, r.text)                          # 500 on main
        self.assertEqual(r.json()["error"], "not_connected")
        self.assertIn("Reconnect Medium (inkcast_connect, or POST /api/v1/platforms/medium/connect-link)", r.json()["message"])
        self.assertEqual(self.ctx.store.get_post(post_id)["status"], "unconfirmed")

    def test_the_mcp_tool_says_the_same(self):
        post_id = self.unconfirmed()

        async def run():
            async with httpx2.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.access}"}) as http:
                async with streamable_http_client(self.cfg.mcp_url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        r = await session.call_tool("inkcast_resend", {"post_id": post_id})
            return r.is_error, "\n".join(c.text for c in r.content if hasattr(c, "text"))

        error, text = asyncio.run(run())
        self.assertTrue(error, text)
        self.assertIn("Medium's stored credentials can't be decrypted", text)
        self.assertIn(f"then resend post {post_id}", text)
        self.assertEqual(self.ctx.store.get_post(post_id)["status"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
