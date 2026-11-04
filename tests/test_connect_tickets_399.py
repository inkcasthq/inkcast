# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A connect ticket (/connect/<ticket>) is used up by exactly one POST (issue #399).

``POST /connect/<ticket>`` read the ticket, verified the credentials with the platform, saved them and only then
deleted the ticket, and nothing made the read and the delete one step: two POSTs with the same ticket both saved.
Now the ticket is used up by one conditional statement (``DELETE ... WHERE ticket_hash = ? AND expires_at > ?``,
which must report one row) before anything is saved, so of two POSTs that both verified, exactly one saves and the
other sees "Link expired".

The rule for a POST whose verification fails: the ticket stays good. The form comes back with the platform's reason
and the same link, so a mistyped key costs a second try, not a new link from the assistant (``test_e2e`` relies on it:
"wrong key shows the error and keeps the link; right key consumes it"). What a leaked link lets its holder do, attach
their own platform account, verifies at the first try either way, so using the link up on a failed verification would
protect nothing; the per-client rate limit on the POST bounds the tries."""

import asyncio
import unittest
from unittest import mock

import httpx2
from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http.app import compose
from inkcast.libs.platforms.devto import DevtoPlatform

PUBLIC_URL = "http://localhost:8787"
GOOD_KEY = "devto-key"


class Dev:
    """DEV's ``/users/me``: the account for ``GOOD_KEY``, 401 for any other key. Counts the verifications."""

    def __init__(self) -> None:
        self.verified = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path == "/api/users/me":
            self.verified += 1
            if request.headers.get("api-key") == GOOD_KEY:
                return httpx2.Response(200, json={"id": 7, "username": "on-call", "name": "On Call"})
            return httpx2.Response(401, json={"error": "unauthorized"})
        return httpx2.Response(404, json={"error": "not found"})


class Case(unittest.TestCase):
    def connector_app(self, dev: Dev):
        """(ASGI app, context) for ConnectorService alone, DEV answered by ``dev``."""
        cfg = Config(public_url=PUBLIC_URL, secret_key="k" * 40, database_path=":memory:", login="password", password="p" * 12)
        specs = resolve("ConnectorService")
        ctx = build(cfg, specs, transport=httpx2.MockTransport(dev))
        self.addCleanup(ctx.store.close)
        return compose(specs, ctx), ctx


def a_ticket(ctx) -> tuple[str, str]:
    """(the person's id, a connect ticket for DEV)."""
    user = ctx.store.upsert_user("local:on-call", "on-call", None, None)["id"]
    return user, ctx.store.create_ticket(user, "devto", ttl=900)


class TwoPostsWithOneTicket(Case):
    def test_two_posts_that_both_verified_save_once(self):
        """Both POSTs have DEV's answer before either reaches the ticket: the consume decides, once."""
        dev = Dev()
        app, ctx = self.connector_app(dev)
        user, ticket = a_ticket(ctx)
        barrier = asyncio.Barrier(2)
        real_verify = DevtoPlatform.verify
        met: list[bool] = []

        async def verify_then_wait(self, creds):
            account = await real_verify(self, creds)
            try:                                         # neither POST goes on until both have verified
                await asyncio.wait_for(barrier.wait(), timeout=5)
                met.append(True)
            except TimeoutError:                         # the other POST never got this far
                met.append(False)
            return account

        async def two_posts():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC_URL) as c:
                with mock.patch.object(DevtoPlatform, "verify", verify_then_wait), \
                        mock.patch.object(ctx.store, "save_credentials", wraps=ctx.store.save_credentials) as saved:
                    first, second = await asyncio.gather(c.post(f"/connect/{ticket}", data={"api_key": GOOD_KEY}),
                                                         c.post(f"/connect/{ticket}", data={"api_key": GOOD_KEY}))
                third = await c.post(f"/connect/{ticket}", data={"api_key": GOOD_KEY})
            return first, second, third, saved.call_count

        first, second, third, saves = asyncio.run(two_posts())
        self.assertEqual(met, [True, True], "both POSTs had DEV's answer before either used the ticket up")
        self.assertEqual(dev.verified, 2)
        self.assertEqual(sorted((first.status_code, second.status_code)), [200, 400], (first.text[:200], second.text[:200]))
        pages = [r.text for r in (first, second)]
        self.assertEqual(sum("DEV connected" in p for p in pages), 1, "exactly one POST connected")
        self.assertEqual(sum("Link expired" in p for p in pages), 1, "the other was told the link was used")
        self.assertEqual(saves, 1, "exactly one POST saved credentials")
        self.assertIsNone(ctx.store.get_ticket(ticket))
        self.assertEqual(third.status_code, 400, "and the ticket is used up for everyone after")
        self.assertEqual(ctx.store.connected_accounts(user)["devto"]["handle"], "on-call")

    def test_a_second_post_after_the_first_saves_nothing(self):
        """The ordinary case of the same rule: the ticket is gone before the first POST's credentials are saved."""
        dev = Dev()
        app, ctx = self.connector_app(dev)
        user, ticket = a_ticket(ctx)
        with TestClient(app, base_url=PUBLIC_URL) as c, \
                mock.patch.object(ctx.store, "save_credentials", wraps=ctx.store.save_credentials) as saved:
            done = c.post(f"/connect/{ticket}", data={"api_key": GOOD_KEY})
            self.assertEqual(done.status_code, 200, done.text[:200])
            self.assertIn("DEV connected", done.text)
            again = c.post(f"/connect/{ticket}", data={"api_key": GOOD_KEY})
            self.assertEqual(again.status_code, 400, again.text[:200])
            self.assertIn("Link expired", again.text)
            self.assertEqual(c.get(f"/connect/{ticket}").status_code, 400)
        self.assertEqual((saved.call_count, dev.verified), (1, 1), "the used ticket is refused before DEV is asked")


class AFailedVerification(Case):
    def test_keeps_the_link_good_for_another_try(self):
        """The rule: a POST DEV refuses shows the form again, with the reason, for the same ticket; the ticket is used
        up by the POST that connected, and by nothing before it."""
        dev = Dev()
        app, ctx = self.connector_app(dev)
        user, ticket = a_ticket(ctx)
        with TestClient(app, base_url=PUBLIC_URL) as c, \
                mock.patch.object(ctx.store, "save_credentials", wraps=ctx.store.save_credentials) as saved:
            wrong = c.post(f"/connect/{ticket}", data={"api_key": "wrong"})
            self.assertEqual(wrong.status_code, 200, wrong.text[:200])
            self.assertIn("<p class=err>", wrong.text, "the form again, with the platform's reason")
            self.assertIn(f'action="/connect/{ticket}"', wrong.text, "for the same ticket")
            self.assertIsNotNone(ctx.store.get_ticket(ticket), "the ticket is still good")
            self.assertEqual(saved.call_count, 0)
            self.assertEqual(ctx.store.connected_accounts(user), {})
            right = c.post(f"/connect/{ticket}", data={"api_key": GOOD_KEY})
            self.assertEqual(right.status_code, 200, right.text[:200])
            self.assertIn("DEV connected", right.text)
            self.assertIsNone(ctx.store.get_ticket(ticket), "used up by the POST that connected")
            self.assertEqual(saved.call_count, 1)
        self.assertEqual(dev.verified, 2)


class ConsumeIsOneConditionalStatement(Case):
    """``VaultRepo.consume_ticket``: True for the one caller that used the ticket up, False for every other, and
    False for an expired ticket, which is left to the purge."""

    def test_true_once_then_false(self):
        _, ctx = self.connector_app(Dev())
        _, ticket = a_ticket(ctx)
        self.assertIs(ctx.store.consume_ticket(ticket), True)
        self.assertIs(ctx.store.consume_ticket(ticket), False)
        self.assertIs(ctx.store.consume_ticket("tkt_never_issued"), False)

    def test_an_expired_ticket_is_not_consumed(self):
        _, ctx = self.connector_app(Dev())
        user, ticket = a_ticket(ctx)
        ctx.store._exec("UPDATE connect_tickets SET expires_at = ? WHERE user_id = ?", 1.0, user)
        self.assertIs(ctx.store.consume_ticket(ticket), False)
        self.assertEqual(ctx.store._one("SELECT count(*) AS n FROM connect_tickets")["n"], 1, "left to the purge")


if __name__ == "__main__":
    unittest.main()
