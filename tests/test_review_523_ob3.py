# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #554 (#523, OB3): the resend's reply repeats the first post's note, read from the store after the resend.

A send's success is written over whatever the post became while the send was out, a cancel included (#214, #216:
``PostsRepo.record_success``). On a platform Inkcast can't ask (Medium), the first stuck pass marks a post whose send
outlasted STUCK_AFTER unconfirmed at once, so its owner can resend it while that first send is still out. When the
first send's answer lands after the resend cancelled the post, the post is on the platform after all (published, or a
draft), and its ``error`` is that answer's notes, not the resend's note: the reply then said "The first post is
cancelled: <the platform's note>", and the warning to check for and remove the extra copy was gone, from both clients
and from a retried resend's refusal."""

import asyncio
import contextlib
import io
import json
import time
import unittest
from unittest import mock

import httpx2

from inkcast import cli
from inkcast.libs.http.rest import problem
from inkcast.libs.pipeline import STUCK_AFTER
from inkcast.libs.platforms import PublishResult
from inkcast.services.publishing.mcp_tools import render_resend
from inkcast.services.publishing.usecases import ServiceError

from test_person_decides import Blind, Harness
from test_person_decides_followups import cli_environment

# Medium's own note for a writer in a publication (``MediumPlatform.publish``), as Blind would word it.
DRAFTED = "You're a writer (not an editor) in Pub, so Blind created a draft for the editors to approve."


class Late(Blind):
    """Blind whose first publish is answered only once the resend's own send is out: that answer, a draft with the
    platform's note, lands while the resend is sending, after the resend cancelled the post."""

    def __init__(self):
        super().__init__()
        self.release: asyncio.Event | None = None
        self.answered: asyncio.Event | None = None
        self.calls = 0

    async def publish(self, creds, account, prepared, destination):
        self.calls += 1
        if self.calls == 1:
            await self.release.wait()
            self.sent.append(prepared.title)
            return PublishResult(url="https://blind/first", remote_id="first", status="draft", notes=[DRAFTED])
        self.release.set()
        await self.answered.wait()
        return await super().publish(creds, account, prepared, destination)


class FindingALateAnswerOfTheFirstSendKeepsTheWarning(Harness):
    platform_class = Late

    def resend_while_the_first_send_is_out(self) -> tuple[str, object]:
        """The first send outlasts STUCK_AFTER; the stuck pass marks the post unconfirmed (Blind can't be asked); its
        owner resends it, and the first send's answer lands while the resend's send is out."""
        prepared = self.platform.prepare({"title": "t"}, "Body.")
        post_id = self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title="t",
                                         status="scheduled", run_at=time.time() - 1, payload=prepared.to_dict())

        async def scenario():
            self.platform.release, self.platform.answered = asyncio.Event(), asyncio.Event()

            async def first_send():
                try:
                    await self.publisher.run_due(now=time.time())
                finally:
                    self.platform.answered.set()

            sending = asyncio.create_task(first_send())
            while self.platform.calls < 1:
                await asyncio.sleep(0)
            await self.publisher.run_due(now=time.time() + STUCK_AFTER + 1)
            self.assertEqual(self.store.get_post(post_id)["status"], "unconfirmed")
            reply = await self.service.resend(self.user, post_id)
            await sending
            return reply

        reply = asyncio.run(scenario())
        row = self.store.get_post(post_id)
        # The race happened: the first send's answer is the post's record now, over the resend's cancel.
        self.assertEqual((row["status"], row["result_url"], row["resent_as"]), ("draft", "https://blind/first", reply.id))
        self.assertEqual(row["error"], DRAFTED)
        return post_id, reply

    def cli_given(self, status: int, body: dict) -> tuple[int, str, str]:
        def client(base_url: str, token: str) -> httpx2.Client:
            return httpx2.Client(base_url=base_url, transport=httpx2.MockTransport(
                lambda _: httpx2.Response(status, json=body)))

        out, err = io.StringIO(), io.StringIO()
        with cli_environment(), mock.patch.object(cli, "_client", client), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.post_main("resend", ["pst_old"])
        return code, out.getvalue(), err.getvalue()

    def test_the_resend_reply_still_says_the_first_post_may_be_on_the_platform(self):
        """The reply said "The first post is cancelled: You're a writer ... so Blind created a draft": the first post is
        a draft on Blind, beside the resend's copy, and nothing told the person to remove one."""
        old_id, reply = self.resend_while_the_first_send_is_out()
        mcp = render_resend(reply, old_id, self.platforms)
        self.assertIn("may already be on Blind", mcp)
        self.assertIn("remove the extra copy", mcp)
        self.assertNotIn(DRAFTED, mcp)
        code, out, _ = self.cli_given(200, reply.model_dump(mode="json"))
        self.assertEqual(code, 0)
        self.assertIn("may already be on the platform", out)
        self.assertIn("remove the extra copy", out)
        self.assertNotIn(DRAFTED, out)

    def test_a_retried_resend_is_not_told_the_platforms_note_as_the_cancels(self):
        """The refusal said "Post ... is draft; only unconfirmed posts can be resent", then "The first post is
        cancelled: You're a writer ...": a post the refusal itself names as a draft, said to be cancelled."""
        old_id, reply = self.resend_while_the_first_send_is_out()
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_resendable"))
        self.assertIn(f"Post {old_id} is draft", caught.exception.message)
        self.assertEqual(caught.exception.extra["post_id"], reply.id)
        self.assertIsNone(caught.exception.extra.get("first_post_note"))
        body = json.loads(problem(409, caught.exception.code, caught.exception.message, **caught.exception.extra).body)
        code, out, err = self.cli_given(409, body)
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn(DRAFTED, err)
        self.assertNotIn("is cancelled:", err)


if __name__ == "__main__":
    unittest.main()
