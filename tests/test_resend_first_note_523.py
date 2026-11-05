# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #523: the resend's replies, on MCP and the CLI, repeat the first post's own note.

Since #502 Inkcast knows when a post was never sent: its credentials could not be decrypted when it was due, on a
platform it can't ask (Medium). The first post's note says so when its owner resends it, but the replies the owner
reads at that moment were hard-coded: "it may already be on ...: check, and remove the extra copy", a copy Inkcast knows
was never made. The resend's REST reply now carries the first post's note (``first_post_note``, added beside every
field the reply had before, AIP-180), and each client prints it in place of that line. A reply without it, from a server
before this field, keeps the line: a client never says less than it knows. A retried resend, its first reply lost, is
told the note too, beside the post the first resend made.

The front doors are driven over real HTTP: REST, the MCP tool and ``inkcast resend``, against a PublishingApi whose
platform can't be asked whether a post arrived (``Blind``); a never-sent post is made the way #502's is, by a scheduler
on another INKCAST_SECRET_KEY and its stuck pass. Each ``Finding`` test failed on main (058a325c); the ``Held`` tests
pin what stays."""

import asyncio
import contextlib
import io
import json
import tempfile
import time
import unittest
from unittest import mock

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from inkcast import cli
from inkcast.launcher import build, resolve
from inkcast.libs.contracts.models import PostInfo
from inkcast.libs.contracts.openapi import openapi_document
from inkcast.libs.db import Store
from inkcast.libs.http.app import compose
from inkcast.libs.observability import Metrics
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.services.publishing.mcp_tools import render_resend

from test_e2e import free_port, serve
from test_one_time_link_setting import by_hand, oauth_token
from test_person_decides import Blind
from test_person_decides_followups import cli_environment

KEY, OTHER_KEY = "k" * 48, "j" * 40          # the API's key; the key a wrong scheduler runs on
NOT_SENT = "so it was not sent"
MAYBE_OUT = "may already be on"
UNSENT_MARKER = ("Stored credentials could not be decrypted when this post was due (they were sealed under another "
                 "INKCAST_SECRET_KEY than the scheduler runs on), so it was not sent.")
# The lines every resend printed before this field, and still prints from a reply without it.
MCP_WARNING = "  The first post is cancelled; it may already be on Blind: check Blind, and remove the extra copy if it is."
CLI_WARNING = ("Post pst_old is cancelled; it may already be on the platform from the first send: check, and remove the "
               "extra copy if it is.")


def new_post() -> dict:
    """The new post as every resend answered it before this field: ``PostInfo``'s fields alone."""
    return {"id": "pst_new", "group_id": "g", "platform": "blind", "title": "t", "status": "published",
            "run_at": "2026-11-05T09:00:00+00:00", "url": "https://blind/1", "error": None, "attempts": 1}


class FrontDoors(unittest.TestCase):
    """One PublishingApi over real HTTP, its registry holding ``Blind`` (no ``find``, like Medium), connected under the
    API's key. ``never_sent`` is #502's post: a scheduler on another key could not begin its send, and its stuck pass
    marked it unconfirmed, saying it was not sent."""

    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.path = tempfile.mkdtemp() + "/inkcast.db"
        cls.cfg = by_hand(public_url=f"http://127.0.0.1:{port}", port=port, secret_key=KEY, database_path=cls.path)
        cls.cfg.validate()
        specs = resolve("PublishingApi")
        cls.ctx = build(cls.cfg, specs)
        cls.blind = Blind()
        cls.ctx.platforms[cls.blind.id] = cls.blind                     # the registry the service, MCP and REST share
        cls.server = serve(compose(specs, cls.ctx), port)
        cls.access, cls.user = oauth_token(cls.ctx.store)
        cls.pat = cls.ctx.store.create_api_token(cls.user, "laptop", ["posts:write"], None)[0]
        cls.ctx.store.save_credentials(cls.user, cls.blind.id, {"k": "v"}, {"id": "1", "name": "blind"})

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.ctx.store.close()

    def post(self, status: str, **fields) -> str:
        prepared = self.blind.prepare({"title": "t"}, "Body.")
        post_id = self.ctx.store.create_post(group_id="g", user_id=self.user, platform=self.blind.id, title="t",
                                           status=status, run_at=time.time() - 1, payload=prepared.to_dict())
        if fields:
            self.ctx.store.update_post(post_id, **fields)
        return post_id

    def never_sent(self) -> str:
        wrong = Store(self.path, OTHER_KEY)
        self.addCleanup(wrong.close)
        scheduler = Publisher(wrong, self.ctx.platforms, metrics=Metrics())
        post_id = self.post("scheduled")
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING"):
            asyncio.run(scheduler.run_due(now=time.time()))
            asyncio.run(scheduler.run_due(now=time.time() + STUCK_AFTER + 1))
        row = self.ctx.store.get_post(post_id)
        self.assertEqual(row["status"], "unconfirmed")
        self.assertTrue(row["error"].startswith(UNSENT_MARKER + " Reconnect Blind"), row["error"])
        return post_id

    def maybe_out(self) -> str:
        """An unconfirmed post whose send began: the platform's answer was lost after the request went out."""
        return self.post("unconfirmed", error="Blind timed out after the request was sent.")

    def note(self, post_id: str) -> str:
        """The first post's note: its error, written by the resend that cancelled it."""
        row = self.ctx.store.get_post(post_id)
        self.assertEqual(row["status"], "cancelled")
        return row["error"]

    def rest(self, post_id: str) -> httpx2.Response:
        return httpx2.post(f"{self.cfg.public_url}/api/v1/posts/{post_id}/resend",
                           headers={"Authorization": f"Bearer {self.access}"}, timeout=30)

    def mcp(self, post_id: str) -> tuple[bool, str]:
        async def run():
            async with httpx2.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.access}"}) as http:
                async with streamable_http_client(self.cfg.mcp_url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        r = await session.call_tool("inkcast_resend", {"post_id": post_id})
            return r.is_error, "\n".join(c.text for c in r.content if hasattr(c, "text"))
        return asyncio.run(run())

    def cli(self, post_id: str, *flags: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with cli_environment(INKCAST_URL=self.cfg.public_url, INKCAST_TOKEN=self.pat), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.post_main("resend", [post_id, *flags])
        return code, out.getvalue(), err.getvalue()


class FindingANeverSentPostIsNotSaidToBeOnThePlatform(FrontDoors):
    def test_the_mcp_reply_carries_the_never_sent_note(self):
        """The reply said "The first post is cancelled; it may already be on Blind: check Blind, and remove the extra
        copy if it is", of a post Inkcast knows was never sent."""
        old_id = self.never_sent()
        sent = len(self.blind.sent)
        error, reply = self.mcp(old_id)
        self.assertFalse(error, reply)
        self.assertIn(": published → https://findable/", reply)
        self.assertEqual(len(self.blind.sent), sent + 1)                     # the one send, nothing before it
        self.assertNotIn(MAYBE_OUT, reply)                                  # the hard-coded line on main
        self.assertNotIn("extra copy", reply)
        self.assertIn(NOT_SENT, reply)
        self.assertIn(f"\n  The first post is cancelled: {self.note(old_id)}", reply)

    def test_the_cli_output_carries_the_never_sent_note(self):
        old_id = self.never_sent()
        code, out, err = self.cli(old_id)
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"^published  https://findable/\d+  \(post pst_\w+\)\n")
        self.assertNotIn(MAYBE_OUT, out)                                    # the hard-coded line on main
        self.assertNotIn("extra copy", out)
        self.assertIn(f"Post {old_id} is cancelled: {self.note(old_id)}", out)
        self.assertIn(NOT_SENT, out)


class HeldEveryOtherResendKeepsTheWarning(FrontDoors):
    """What the warning said before this field, the reply still says: the words both versions share."""

    def test_the_mcp_reply_says_the_first_post_may_already_be_on_the_platform(self):
        old_id = self.maybe_out()
        error, reply = self.mcp(old_id)
        self.assertFalse(error, reply)
        self.assertIn("The first post is cancelled", reply)
        self.assertIn("may already be on Blind", reply)
        self.assertRegex(reply, r"[Cc]heck Blind, and remove the extra copy")
        self.assertNotIn(NOT_SENT, reply)

    def test_the_cli_output_says_the_first_post_may_already_be_on_the_platform(self):
        old_id = self.maybe_out()
        code, out, err = self.cli(old_id)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"Post {old_id} is cancelled", out)
        self.assertIn("may already be on", out)
        self.assertIn("from the first send", out)
        self.assertIn("remove the extra copy", out)
        self.assertNotIn(NOT_SENT, out)


class FindingTheRestReplyCarriesTheFirstPostsNote(FrontDoors):
    def test_the_reply_is_the_new_post_with_every_field_and_the_first_posts_note(self):
        """The reply was the new post alone (``PostInfo``): no client could tell a never-sent first post from one that
        may be on the platform."""
        for make in (self.never_sent, self.maybe_out):
            with self.subTest(first=make.__name__):
                old_id = make()
                r = self.rest(old_id)
                self.assertEqual(r.status_code, 200, r.text)
                body = r.json()
                self.assertEqual(body.get("first_post_note"), self.note(old_id))       # absent on main
                self.assertEqual(set(body), set(PostInfo.model_fields) | {"first_post_note"})
                listed = httpx2.get(f"{self.cfg.public_url}/api/v1/posts",
                                    headers={"Authorization": f"Bearer {self.access}"}, timeout=30).json()
                [new] = [p for p in listed if p["id"] == body["id"]]
                self.assertEqual({k: body[k] for k in PostInfo.model_fields}, new)  # every field as the listing has it

    def test_json_passes_the_field_through(self):
        old_id = self.never_sent()
        code, out, err = self.cli(old_id, "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out).get("first_post_note"), self.note(old_id))

    def test_the_api_document_lists_it_and_keeps_every_field_of_the_new_post(self):
        for doc in (openapi_document(self.cfg.public_url),
                    httpx2.get(f"{self.cfg.public_url}/api/openapi.json", timeout=30).json()):
            schemas = doc["components"]["schemas"]
            ok = doc["paths"]["/api/v1/posts/{post_id}/resend"]["post"]["responses"]["200"]
            ref = ok["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[1]
            self.assertNotEqual(ref, "PostInfo", "the resend still answers PostInfo alone")
            resent, post = schemas[ref], schemas["PostInfo"]
            self.assertIn("first_post_note", resent["properties"])
            self.assertEqual(resent["properties"]["first_post_note"]["anyOf"], [{"type": "string"}, {"type": "null"}])
            self.assertNotIn("first_post_note", resent.get("required", []))
            self.assertEqual({k: v for k, v in resent["properties"].items() if k != "first_post_note"},
                             post["properties"])                         # AIP-180: every field, as it was
            self.assertEqual(resent.get("required"), post.get("required"))
            self.assertNotIn("first_post_note", post["properties"], "every posts route would carry it")
            self.assertIn("first_post_note", schemas["ErrorBody"]["properties"])


class FindingARetriedResendIsToldTheFirstPostsNote(FrontDoors):
    def test_the_409_carries_it_beside_the_new_post_and_both_clients_print_it(self):
        """The first reply was lost: the retry is told the post the first resend made (#234), and now what the lost
        reply said of the first post too, which a retry whose first reply said "may already be on" never heard."""
        for make in (self.never_sent, self.maybe_out):
            with self.subTest(first=make.__name__):
                old_id = make()
                first = self.rest(old_id).json()
                again = self.rest(old_id)
                self.assertEqual(again.status_code, 409, again.text)
                body = again.json()
                self.assertEqual((body["error"], body["post_id"]), ("not_resendable", first["id"]))
                self.assertEqual(body.get("first_post_note"), self.note(old_id))
                self.assertEqual(body["first_post_note"], first["first_post_note"])
                code, out, err = self.cli(old_id)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"inkcast resend: 409: Post {old_id} is cancelled; only unconfirmed posts can be resent.",
                              err)
                self.assertIn(f"\nPost {old_id} is cancelled: {self.note(old_id)}\n", err)
                error, text = self.mcp(old_id)
                self.assertTrue(error, text)
                self.assertIn(f"It was resent as post {first['id']}, which is published: follow that one.", text)
                self.assertIn(f"\n  The first post is cancelled: {self.note(old_id)}", text)


class HeldARefusalOfAPostNotResentIsAsBefore(FrontDoors):
    def test_a_post_cancelled_by_hand_carries_no_note(self):
        """Only a resent post's 409 names a new post and its note; the cancel's own reply said the note already."""
        old_id = self.maybe_out()
        httpx2.post(f"{self.cfg.public_url}/api/v1/posts/{old_id}/cancel",
                    headers={"Authorization": f"Bearer {self.access}"}, timeout=30).raise_for_status()
        refused = self.rest(old_id)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertNotIn("post_id", refused.json())
        self.assertNotIn("first_post_note", refused.json())
        code, out, err = self.cli(old_id)
        self.assertEqual((code, out, err.count("\n")), (1, "", 1))


class HeldAReplyWithoutTheFieldKeepsTodaysWarning(unittest.TestCase):
    """A server before #523 answers the new post alone; the MCP tool's ``render_resend`` given a ``PostInfo``."""

    def cli(self, body: dict, *flags: str) -> tuple[int, str]:
        def client(base_url: str, token: str) -> httpx2.Client:
            return httpx2.Client(base_url=base_url, transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=body)))

        out = io.StringIO()
        with cli_environment(), mock.patch.object(cli, "_client", client), contextlib.redirect_stdout(out):
            code = cli.post_main("resend", ["pst_old", *flags])
        return code, out.getvalue()

    def test_the_cli_prints_the_warning(self):
        code, out = self.cli(new_post())
        self.assertEqual((code, out), (0, f"published  https://blind/1  (post pst_new)\n{CLI_WARNING}\n"))
        for empty in (None, ""):                                          # a field with nothing in it says nothing
            with self.subTest(first_post_note=empty):
                self.assertEqual(self.cli({**new_post(), "first_post_note": empty})[1].splitlines()[-1], CLI_WARNING)

    def test_json_prints_the_reply_as_it_came(self):
        code, out = self.cli(new_post(), "--json")
        self.assertEqual((code, json.loads(out)), (0, new_post()))

    def test_the_mcp_reply_prints_the_warning(self):
        reply = render_resend(PostInfo(**new_post()), "pst_old", {"blind": Blind()})
        self.assertEqual(reply, f"Resent “t” (post `pst_old`) as post `pst_new`: published → https://blind/1\n{MCP_WARNING}")


class FindingTheMcpReplyReadsTheField(unittest.TestCase):
    def test_a_note_prints_in_place_of_the_warning_and_an_empty_one_keeps_it(self):
        from inkcast.libs.contracts.models import ResendResponse          # the reply's model, new with the field

        post = new_post()
        registry = {"blind": Blind()}
        noted = render_resend(ResendResponse(**post, first_post_note=f"Resent by the person as post pst_new. {UNSENT_MARKER}"),
                              "pst_old", registry)
        self.assertEqual(noted.splitlines()[-1],
                         f"  The first post is cancelled: Resent by the person as post pst_new. {UNSENT_MARKER}")
        for empty in (None, ""):
            with self.subTest(first_post_note=empty):
                self.assertEqual(render_resend(ResendResponse(**post, first_post_note=empty), "pst_old", registry)
                                 .splitlines()[-1], MCP_WARNING)


if __name__ == "__main__":
    unittest.main()
