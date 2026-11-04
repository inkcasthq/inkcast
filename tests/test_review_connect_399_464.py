# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #464 (issue #399): a connect ticket is used up by the POST that stores a key, and by nothing else.

The PR consumes the ticket with one conditional statement and then saves the credentials with another, each in
autocommit. A save that fails after the consume (the database refuses the write: full, read-only, busy past its
timeout) leaves the ticket gone and nothing stored: the person sees a server error and their link is spent, although
docs/architecture.md says the link "is used up by the one POST that stores a key" and a refused key leaves it good.
The consume and the save belong in one transaction, so a failed save rolls the consume back.

The failure is made by the database itself (a trigger that aborts the INSERT into ``credentials``), not by mocking a
method, so the test holds whatever shape the fix takes. The POST carries the form's token when the page has one
(issue #401, PR #468), so the test runs unchanged on top of that PR."""

import re
import unittest

from starlette.testclient import TestClient

from test_connect_tickets_399 import GOOD_KEY, PUBLIC_URL, Case, Dev, a_ticket


def form_data(page: str, **fields: str) -> dict[str, str]:
    """The fields, plus the form's token when the connect page carries one (issue #401)."""
    token = re.search(r'name=csrf value="([^"]*)"', page)
    return {**fields, **({"csrf": token.group(1)} if token else {})}


class AFailedSaveLeavesTheLinkGood(Case):
    def test_a_save_the_database_refuses_does_not_use_the_link_up(self):
        dev = Dev()
        app, ctx = self.connector_app(dev)
        user, ticket = a_ticket(ctx)
        ctx.store._exec("CREATE TEMP TRIGGER r46y_refuse BEFORE INSERT ON credentials "
                        "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END")
        with TestClient(app, base_url=PUBLIC_URL, raise_server_exceptions=False) as c:
            page = c.get(f"/connect/{ticket}").text
            failed = c.post(f"/connect/{ticket}", data=form_data(page, api_key=GOOD_KEY))
            self.assertEqual(failed.status_code, 500, failed.text[:200])
            self.assertEqual(ctx.store.connected_accounts(user), {}, "nothing was stored")
            self.assertIsNotNone(ctx.store.get_ticket(ticket), "so the link was not used up: another try is allowed")

            ctx.store._exec("DROP TRIGGER r46y_refuse")
            page = c.get(f"/connect/{ticket}")
            self.assertEqual(page.status_code, 200, "the person reopens the same link")
            done = c.post(f"/connect/{ticket}", data=form_data(page.text, api_key=GOOD_KEY))
            self.assertEqual(done.status_code, 200, done.text[:200])
            self.assertIn("DEV connected", done.text)
        self.assertIsNone(ctx.store.get_ticket(ticket), "used up by the POST that stored the key")
        self.assertEqual(ctx.store.connected_accounts(user)["devto"]["handle"], "on-call")


if __name__ == "__main__":
    unittest.main()
