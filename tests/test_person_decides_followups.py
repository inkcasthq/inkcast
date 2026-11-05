# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #234: the follow-ups from the review of #231 (#226, a person decides on an unconfirmed post).

One ``Finding`` class per item; each class's first test failed on main before the fix and passes with it, and the tests
after it pin what holds. Concurrency is driven by hooks and asyncio barriers inside the platform's calls, never by
sleeps: another worker is a second ``Publisher`` on its own connection, run as a task while the first worker's request
is held inside the platform's ``publish`` or ``find_existing``.

1. recovery's found answer is kept over a post that went 'unconfirmed' while the lookup was out;
2. the cap asks the platform one last time, and records a post it has;
3. a re-send's success keeps the note that an earlier attempt's answer was lost (a second copy may be there);
4. a resend is refused, before anything is cancelled, while the platform isn't connected or refuses the credentials;
5. the 409 for a retried resend names the post the first resend made;
6. ``render_resend`` names every status the new post can have;
8. ``inkcast resend`` and ``inkcast cancel``; ``inkcast sync --fail-on-decision``;
9. the resend's ledger write goes through the publisher's store;
10. ``repoint_entries`` moves only the person's own entries;
11. the wording, and the ``post.cancelled`` event of a resent post, applied once by the KPI rollup."""

import asyncio
import contextlib
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import httpx2

from inkcast.libs.auth import SCOPES
from inkcast.libs.contracts.models import PostInfo
from inkcast.libs.kpi import RollupEngine, bucket_start
from inkcast.libs.pipeline import STUCK_AFTER
from inkcast.libs.platforms import PlatformError
from inkcast.services.publishing import sync as sync_module
from inkcast.services.publishing.mcp_tools import render_resend
from inkcast.services.publishing.usecases import InkcastService, ServiceError

from test_person_decides import DAY, PATH, ROOT, SOURCE, Blind, Findable, Harness, SyncHarness


class Refusing(Findable):
    """Findable whose ``verify`` answers as a plug-in's does when the platform rejects the stored key (a 401), or can't
    be asked just now (a 429): ``verify_answer`` is raised by the next ``verify``."""

    def __init__(self):
        super().__init__()
        self.verify_answer: PlatformError | None = None
        self.verified = 0

    async def verify(self, creds):
        self.verified += 1
        if self.verify_answer is not None:
            answer, self.verify_answer = self.verify_answer, None
            raise answer
        return await super().verify(creds)


# ---- 1. recovery's found answer is kept over a post unconfirmed meanwhile ----------------------------------------


class FindingAFoundAnswerIsKeptOverAPostUnconfirmedMeanwhile(Harness):
    def test_the_platforms_word_that_it_has_the_post_is_not_dropped_for_a_late_uncertain_answer(self):
        """Worker A's request reached Findable; its answer is slow. Worker B's stuck pass takes the post over and asks
        Findable, which says it has it. While that lookup is still out, A's answer lands: a timeout after the request
        was sent, written over 'recovering' as 'unconfirmed' (it yields only to a cancel, or to a found answer already
        written). B's found answer was then dropped (``only_if="recovering"``): a post the platform has stayed
        'unconfirmed', the alert fired, and the person was asked to resend it, a second copy."""
        post_id = self.post("scheduled")
        b = self.replica()
        b_asking, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def b_asks(prepared):                                           # inside B's lookup: hold its answer
            b_asking.set()
            await release.wait()

        async def while_a_waits(prepared):                                    # inside A's publish
            self.platform.sent.append(prepared.title)                         # A's request reached Findable
            self.platform.meanwhile = b_asks
            self.tasks.append(asyncio.create_task(b.run_due(now=time.time() + STUCK_AFTER + 1)))
            await b_asking.wait()
            seen.append(("b asking", self.status(post_id)))
            raise PlatformError("Findable timed out after the request was sent.", uncertain=True)

        async def run():
            self.tasks = []
            await self.publisher.run_due(now=time.time())                     # A's tick
            seen.append(("a done", self.status(post_id)))
            release.set()
            return await asyncio.gather(*self.tasks)

        self.platform.during_publish = while_a_waits
        ([recovered],) = asyncio.run(run())
        self.assertEqual(seen, [("b asking", "recovering"), ("a done", "unconfirmed")])
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], post["remote_id"]),
                         ("published", "https://findable/found", "found"), "the found answer was dropped")
        self.assertEqual((recovered.status, recovered.url), ("published", "https://findable/found"))
        self.assertIsNone(post["error"])
        self.assertIn(("post.recovered", self.user, {"platform": "findable", "status": "published"}), self.events(post_id))
        with self.assertRaises(ServiceError) as caught:                      # nothing for the person to decide
            asyncio.run(self.service.resend(self.user, post_id))
        self.assertEqual(caught.exception.code, "not_resendable")
        self.assertEqual(self.platform.sent, ["t"], "sent twice")

    def test_a_cancel_made_while_the_lookup_was_out_still_holds(self):
        post_id = self.post("publishing")
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        self.platform.sent.append("t")
        api = self.replica()

        async def the_person_cancels(prepared):
            api.cancel(self.user, post_id)

        self.platform.meanwhile = the_person_cancels
        (outcome,) = self.tick()
        self.assertEqual((outcome.status, self.status(post_id)), ("cancelled", "cancelled"))


# ---- 2. the cap asks the platform one last time --------------------------------------------------------------------


class FindingTheCapAsksThePlatformOneLastTime(Harness):
    def past_the_cap(self, title: str = "t") -> str:
        """A post 'recovering' for two days (interrupted, re-sent, interrupted again: the first clock is kept, #226)."""
        post_id = self.post("recovering", title)
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?, recovering_since = ? WHERE id = ?",
                         STUCK_AFTER + 1, time.time() - 2 * DAY, post_id)
        return post_id

    def test_a_post_the_platform_has_is_recorded_not_left_to_the_person(self):
        """The cap marked a post 'unconfirmed' without asking. With the clock kept across a re-entry, a post re-sent
        after a "missing" and interrupted again past the cap was never asked whether its second send arrived: the
        person was told to check and resend a post the platform had."""
        post_id = self.past_the_cap()
        self.platform.sent.append("t")                                        # the interrupted send had arrived
        (outcome,) = self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], post["result_url"], post["remote_id"]),
                         ("published", "published", "https://findable/found", "found"), "unconfirmed without asking")
        self.assertEqual((self.platform.lookups, self.platform.sent), (1, ["t"]))
        self.assertIn(("post.recovered", self.user, {"platform": "findable", "status": "published"}), self.events(post_id))
        self.assertEqual(self.tick(later=2 * DAY), [])

    def test_missing_and_refused_still_end_unconfirmed_and_nothing_is_sent_on_its_own(self):
        """Past the cap, only a found answer is recorded: "missing" is not sent again (the cap bounds the lateness,
        and a lookup can lag the post), and the person's note says what the platform said."""
        for how in ("missing", "refused"):
            with self.subTest(how=how):
                post_id = self.past_the_cap(how)
                self.platform.refuse = how == "refused"
                (outcome,) = self.tick()
                self.platform.refuse = False
                post = self.store.get_post(post_id)
                self.assertEqual((outcome.status, post["status"], self.platform.sent), ("unconfirmed", "unconfirmed", []))
                self.assertIn("INKCAST_PUBLISH_RECOVERY_MAX_HOURS", post["error"])
                self.assertIn(f"resend it (inkcast_resend, or POST /api/v1/posts/{post_id}/resend)", post["error"])
                self.assertIn(f"cancel it (inkcast_cancel, or POST /api/v1/posts/{post_id}/cancel)", post["error"])
                if how == "missing":
                    self.assertIn("Findable says it isn't there", post["error"])
                    self.assertNotIn("could not be asked", post["error"])
                else:
                    self.assertIn("could not be asked whether it arrived for 24 hours", post["error"])
                self.assertIn(("post.recovered", self.user, {"platform": "findable", "status": "unconfirmed"}),
                              self.events(post_id))
                self.assertEqual(self.tick(later=2 * DAY), [], "sent on its own")
        self.assertEqual(self.platform.lookups, 2, "the cap asked more than once, or not at all")


class HeldABlindPlatformIsCappedWithoutALookup(Harness):
    platform_class = Blind

    def test_the_cap_still_ends_a_post_the_platform_cannot_be_asked_about(self):
        post_id = self.post("recovering")
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?, recovering_since = ? WHERE id = ?",
                         STUCK_AFTER + 1, time.time() - 2 * DAY, post_id)
        (outcome,) = self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], self.platform.lookups), ("unconfirmed", "unconfirmed", 0))
        self.assertIn("could not be asked whether it arrived for 24 hours", post["error"])


# ---- 3. a re-send's success keeps the note that an earlier attempt may have arrived --------------------------------


class FindingASuccessKeepsAnEarlierAttemptsLostAnswer(Harness):
    def test_the_second_copy_flag_survives_the_re_sends_success(self):
        """Worker A's send is out. Worker B's stuck pass takes the post, is told "missing", re-queues it and starts
        sending it (attempt 2). A's answer lands first: a timeout after the request was sent, written over B's
        'publishing' as 'unconfirmed', since this request may have arrived too. Then B's success landed and wrote
        'published' with B's URL and nothing else: a post that may be on Findable twice read as any published post,
        and nobody was told to look for the extra copy."""
        post_id = self.post("scheduled")
        b = self.replica()
        b_out, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def b_sends(prepared):                                          # inside B's publish: hold its request
            b_out.set()
            await release.wait()

        async def while_a_waits(prepared):                                    # inside A's publish
            self.platform.during_publish = b_sends
            self.tasks.append(asyncio.create_task(b.run_due(now=time.time() + STUCK_AFTER + 1)))
            await b_out.wait()
            seen.append(("b out", self.status(post_id), self.store.get_post(post_id)["attempts"]))
            raise PlatformError("Findable timed out after the request was sent.", uncertain=True)

        async def run():
            self.tasks = []
            await self.publisher.run_due(now=time.time())                     # A's tick
            seen.append(("a done", self.status(post_id), self.store.get_post(post_id)["error"]))
            release.set()
            return await asyncio.gather(*self.tasks)

        self.platform.during_publish = while_a_waits
        (b_outcomes,) = asyncio.run(run())                                    # B's recovery, then B's send
        self.assertEqual(seen[0], ("b out", "publishing", 2))
        self.assertEqual(seen[1][:2], ("a done", "unconfirmed"))
        self.assertIn("second copy may be on Findable", seen[1][2], "A's answer over B's re-send doesn't say so")
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], self.platform.sent), ("published", "https://findable/1", ["t"]))
        self.assertIn("second copy may be on Findable", post["error"] or "", "the 'may be there twice' flag was lost")
        self.assertIn("answer was lost after its request was sent", post["error"])
        self.assertTrue(any("second copy may be on Findable" in n for n in b_outcomes[-1].notes), b_outcomes[-1])

    def test_the_other_order_is_unconfirmed_with_the_url_and_the_same_flag(self):
        """B's success lands first, then A's uncertain answer: 'unconfirmed' with B's URL (held since #216), and now
        the same note, so whichever answer lands last the post says a second copy may be there."""
        post_id = self.post("scheduled")
        b = self.replica()

        async def overtaken(prepared):
            await b.run_due(now=time.time() + STUCK_AFTER + 1)                # B: missing → re-queued → sent → published
            raise PlatformError("Findable timed out after the request was sent.", uncertain=True)

        self.platform.during_publish = overtaken
        self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"]), ("unconfirmed", "https://findable/1"))
        self.assertIn("Findable timed out after the request was sent.", post["error"])
        self.assertIn("second copy may be on Findable", post["error"])

    def test_an_uncertain_answer_with_nobody_else_holding_the_post_carries_no_such_note(self):
        post_id = self.post("scheduled")
        self.platform.answer = PlatformError("Findable timed out after the request was sent.", uncertain=True)
        self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["error"]), ("unconfirmed", "Findable timed out after the request was sent."))


# ---- 4. a resend is refused while the platform isn't connected -----------------------------------------------------


class FindingAResendIsRefusedBeforeAnythingIsCancelledWhileDisconnected(Harness):
    platform_class = Refusing

    def test_a_disconnected_platform_refuses_the_resend_and_names_the_reconnect(self):
        """The resend cancelled the old post, then the new one ended 'failed' ("Findable was disconnected."): the
        decision was consumed, and a Git-synced file would be created afresh at the next push."""
        old_id = self.unconfirmed()
        self.store.delete_credentials(self.user, self.platform.id)
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_connected"))
        self.assertIn("Findable isn't connected", caught.exception.message)
        self.assertIn("inkcast_connect", caught.exception.message)
        self.assertIn("POST /api/v1/platforms/findable/connect-link", caught.exception.message)
        self.assertIn(f"then resend post {old_id}", caught.exception.message)
        self.assertEqual((self.status(old_id), len(self.posts()), self.platform.sent), ("unconfirmed", 1, []), "cancelled")
        self.assertEqual([t for t, _, _ in self.events(old_id)], [])
        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
        info = asyncio.run(self.service.resend(self.user, old_id))           # reconnected: the decision goes through
        self.assertEqual((info.status, self.status(old_id), self.platform.sent), ("published", "cancelled", ["t"]))

    def test_credentials_the_platform_refuses_refuse_the_resend_too(self):
        old_id = self.unconfirmed()
        self.platform.verify_answer = PlatformError("Findable rejected the API key (revoked or invalid). Reconnect Findable "
                                                    "in Inkcast.")
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_connected"))
        self.assertIn("Findable rejected the API key", caught.exception.message)
        self.assertIn("inkcast_connect, or POST /api/v1/platforms/findable/connect-link", caught.exception.message)
        self.assertEqual((self.status(old_id), len(self.posts()), self.platform.sent, self.platform.verified),
                         ("unconfirmed", 1, [], 1))

    def test_a_platform_that_cannot_be_asked_just_now_refuses_the_decision_for_now(self):
        """A rate-limited check (a 429 on the account lookup) refuses the resend too, before anything is cancelled: the
        send would very likely get the same 429, and a resend's send is never queued (#445; it let the send say what
        happens before)."""
        old_id = self.unconfirmed()
        self.platform.verify_answer = PlatformError("Findable rate limit hit.", retryable=True, retry_after=60)
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "platform_unavailable"))
        self.assertEqual((self.status(old_id), self.platform.verified, self.platform.sent), ("unconfirmed", 1, []))


# ---- 5. the 409 for a retried resend names the new post --------------------------------------------------------------


class FindingARetriedResendNamesTheNewPost(Harness):
    def test_the_409_names_the_post_the_first_resend_made(self):
        """A client whose first reply was lost retries, and was told only "is cancelled; only unconfirmed posts can be
        resent": nothing named the post the first call made, so it might publish by hand, a second copy."""
        old_id = self.unconfirmed()
        first = asyncio.run(self.service.resend(self.user, old_id))
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_resendable"))
        self.assertIn(f"resent as post {first.id}, which is published", caught.exception.message,
                      "the client whose first reply was lost isn't told the new post")
        self.assertIn("is cancelled; only unconfirmed posts can be resent", caught.exception.message)
        self.assertEqual(caught.exception.extra, {"post_id": first.id,
                                                  "first_post_note": self.store.get_post(old_id)["error"]})
        from inkcast.libs.http.rest import problem
        body = json.loads(problem(caught.exception.status, caught.exception.code, caught.exception.message,
                                  **caught.exception.extra).body)
        self.assertEqual((body["status"], body["error"], body["post_id"]), (409, "not_resendable", first.id))

    def test_a_chain_of_resends_names_each_posts_own_successor(self):
        self.platform.answer = PlatformError("Findable timed out after the request was sent.", uncertain=True)
        old_id = self.unconfirmed("c")
        second = asyncio.run(self.service.resend(self.user, old_id))          # unconfirmed in its turn
        self.assertEqual(second.status, "unconfirmed")
        third = asyncio.run(self.service.resend(self.user, second.id))
        self.assertEqual(third.status, "published")
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual(caught.exception.extra, {"post_id": second.id,
                                                  "first_post_note": self.store.get_post(old_id)["error"]})
        self.assertIn(f"resent as post {second.id}, which is cancelled", caught.exception.message)
        self.assertNotIn(third.id, caught.exception.message)
        self.assertEqual(self.store.get_post(old_id)["resent_as"], second.id)
        self.assertEqual(self.store.get_post(second.id)["resent_as"], third.id)
        self.assertIsNone(self.store.get_post(third.id)["resent_as"])

    def test_a_post_cancelled_by_hand_names_no_new_post(self):
        post_id = self.unconfirmed("x")
        self.service.cancel(self.user, post_id)
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, post_id))
        self.assertEqual((caught.exception.code, caught.exception.extra), ("not_resendable", {}))
        self.assertEqual(caught.exception.message, f"Post {post_id} is cancelled; only unconfirmed posts can be resent.")


# ---- 6. render_resend names every status -------------------------------------------------------------------------------


class FindingRenderResendNamesEveryStatus(unittest.TestCase):
    def info(self, status: str, error: str) -> PostInfo:
        return PostInfo(id="pst_new", group_id="g", platform="findable", title="t", status=status,
                        run_at="2026-11-04T09:00:00+00:00", error=error)

    def test_a_resend_taken_over_as_stuck_is_not_called_failed(self):
        """A resend whose send outlasted STUCK_AFTER is 'recovering' once another worker takes it over: the reply
        called it FAILED, with recovery's "will be checked again" as the reason."""
        registry = {"findable": Findable()}
        reply = render_resend(self.info("recovering", "Could not check Findable yet (rate limit); it will be checked again."),
                              "pst_old", registry)
        self.assertIn("RECOVERING — Could not check Findable yet", reply)
        self.assertNotIn("FAILED", reply)
        failed = render_resend(self.info("failed", "Findable refused the post (400)."), "pst_old", registry)
        self.assertIn("FAILED — Findable refused the post (400).", failed)
        for status in ("published", "draft", "unlisted", "unconfirmed", "scheduled", "cancelled"):
            with self.subTest(status=status):
                reply = render_resend(self.info(status, "note"), "pst_old", registry)
                self.assertIn(status if status in ("published", "draft", "unlisted") else status.upper(), reply)
                self.assertNotIn("FAILED", reply)


# ---- 8. the CLI ----------------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def cli_environment(directory: pathlib.Path | None = None, **env: str):
    """The CLI's environment: INKCAST_URL and a personal access token, and the current directory when given."""
    old_env, old_cwd = dict(os.environ), os.getcwd()
    try:
        os.environ.update({"INKCAST_URL": "https://inkcast.example.com", "INKCAST_TOKEN": "ink_pat_test", **env})
        for name in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
            os.environ.pop(name, None)
        if directory is not None:
            os.chdir(directory)
        yield
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


class FindingTheCliResendsAndCancels(unittest.TestCase):
    """``inkcast resend <post-id>`` and ``inkcast cancel <post-id>``, against a stand-in server (a mock transport),
    so every reply the API gives is driven: the exit code and what is printed."""

    def run_cli(self, command: str, argv: list[str], reply) -> tuple[int, str, str, list[httpx2.Request]]:
        from inkcast import cli

        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            status, body = reply(request) if callable(reply) else reply
            return httpx2.Response(status, json=body)

        def client(base_url: str, token: str) -> httpx2.Client:
            return httpx2.Client(base_url=base_url, transport=httpx2.MockTransport(handler),
                                 headers={"Authorization": f"Bearer {token}"})

        out, err = io.StringIO(), io.StringIO()
        with cli_environment(), mock.patch.object(cli, "_client", client), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.post_main(command, argv)
        return code, out.getvalue(), err.getvalue(), requests

    def post(self, status: str, error: str | None = None, url: str | None = None) -> dict:
        return {"id": "pst_new", "group_id": "g", "platform": "devto", "title": "Hello", "status": status,
                "run_at": "2026-11-04T09:00:00+00:00", "url": url, "error": error, "attempts": 1}

    def test_resend_posts_to_the_route_and_prints_the_new_post(self):
        code, out, err, requests = self.run_cli("resend", ["pst_old"], (200, self.post("published", url="https://dev.to/x")))
        self.assertEqual((code, err), (0, ""))
        [request] = requests
        self.assertEqual((request.method, request.url.path), ("POST", "/api/v1/posts/pst_old/resend"))
        self.assertEqual(request.headers["authorization"], "Bearer ink_pat_test")
        self.assertIn("published  https://dev.to/x  (post pst_new)", out)
        self.assertIn("Post pst_old is cancelled", out)

    def test_a_resend_that_is_not_published_exits_1_and_says_why(self):
        for status, error in (("unconfirmed", "DEV timed out after the request was sent."),
                              ("failed", "DEV refused the post (400)."),
                              ("recovering", "Could not check DEV yet; it will be checked again.")):
            with self.subTest(status=status):
                code, out, err, _ = self.run_cli("resend", ["pst_old"], (200, self.post(status, error=error)))
                self.assertEqual(code, 1)
                self.assertIn(f"{status}  (post pst_new)", out)
                self.assertIn(error, out)

    def test_cancel_posts_to_the_route_and_repeats_the_note(self):
        note = "Cancelled while DEV had not said whether it arrived: it may already be on DEV."
        code, out, err, requests = self.run_cli("cancel", ["pst_x"], (200, self.post("cancelled", error=note)))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(requests[0].url.path, "/api/v1/posts/pst_x/cancel")
        self.assertIn("cancelled  (post pst_new)", out)
        self.assertIn(note, out)

    def test_the_apis_refusals_become_exit_codes_with_the_message(self):
        problems = [((409, "not_resendable", {"post_id": "pst_second"}), 1), ((404, "not_found", {}), 1),
                    ((409, "not_connected", {}), 1), ((403, "insufficient_scope", {}), 2), ((401, "invalid_token", {}), 2)]
        for (status, error, extra), expected in problems:
            with self.subTest(status=status, error=error):
                body = {"error": error, "message": f"The API said {error}.", "status": status, **extra}
                code, out, err, _ = self.run_cli("resend", ["pst_old"], (status, body))
                self.assertEqual((code, out), (expected, ""))
                self.assertIn(f"inkcast resend: {status}: The API said {error}.", err)

    def test_json_prints_the_reply_and_a_missing_token_is_a_usage_error(self):
        code, out, _, _ = self.run_cli("cancel", ["pst_x", "--json"], (200, self.post("cancelled")))
        self.assertEqual((code, json.loads(out)["status"]), (0, "cancelled"))
        from inkcast import cli
        err = io.StringIO()
        with cli_environment(INKCAST_TOKEN=""), contextlib.redirect_stderr(err):
            self.assertEqual(cli.post_main("resend", ["pst_x"]), 2)
        self.assertIn("INKCAST_TOKEN", err.getvalue())
        with cli_environment(INKCAST_URL="http://inkcast.example.com"), contextlib.redirect_stderr(err):
            self.assertEqual(cli.post_main("cancel", ["pst_x"]), 2)
        self.assertIn("plain http", err.getvalue())

    def test_the_launcher_dispatches_both_commands(self):
        from inkcast import launcher
        with mock.patch("inkcast.cli.post_main", return_value=7) as post_main:
            with self.assertRaises(SystemExit) as stopped:
                launcher.main(["resend", "pst_x"])
            self.assertEqual((stopped.exception.code, post_main.call_args.args), (7, ("resend", ["pst_x"])))
            with self.assertRaises(SystemExit) as stopped:
                launcher.main(["cancel", "pst_y", "--json"])
            self.assertEqual((stopped.exception.code, post_main.call_args.args), (7, ("cancel", ["pst_y", "--json"])))


class FindingSyncCanFailOnAFileThatWaitsForADecision(unittest.TestCase):
    """``inkcast sync`` exits 0 on a 'skipped' result, so CI never flagged a file waiting for the person. Behind
    ``--fail-on-decision`` it exits 1 for one (and only one): the default exit code is unchanged, since a decision
    only a person can make must not fail every push until it is made unless the repository asks for that."""

    def run_sync(self, results: list[dict], *flags: str) -> tuple[int, str]:
        from inkcast import cli

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json={"source": "s", "ref": None, "dry_run": False, "results": results})

        def client(base_url: str, token: str) -> httpx2.Client:
            return httpx2.Client(base_url=base_url, transport=httpx2.MockTransport(handler))

        repo = pathlib.Path(tempfile.mkdtemp())
        (repo / "posts").mkdir()
        (repo / "posts" / "a.md").write_text("---\ntitle: A\n---\n\nBody.\n", encoding="utf-8")
        out = io.StringIO()
        with cli_environment(repo), mock.patch.object(cli, "_client", client), contextlib.redirect_stdout(out):
            code = cli.sync_main([*flags, "posts/*.md", "--source", "github.com/acme/blog"])
        return code, out.getvalue()

    @staticmethod
    def result(action: str, status: str | None, message: str) -> dict:
        return {"path": "posts/a.md", "platform": "devto", "action": action, "status": status, "post_id": "pst_1",
                "url": None, "message": message, "preview": None}

    def test_a_decision_fails_the_sync_only_behind_the_flag(self):
        for status in ("unconfirmed", "recovering"):
            with self.subTest(status=status):
                waiting = [self.result("skipped", status, f"The last publish to DEV may or may not have gone through ({status}).")]
                self.assertEqual(self.run_sync(waiting)[0], 0, "today's exit code changed")
                code, out = self.run_sync(waiting, "--fail-on-decision")
                self.assertEqual(code, 1)
                self.assertIn("- skipped", out)

    def test_other_skipped_results_do_not_fail_behind_the_flag(self):
        for status, message in ((None, "DEV isn't connected."), ("syncing", "Another sync of this file is in progress."),
                                ("publishing", "Its post is being sent to DEV right now.")):
            with self.subTest(message=message):
                code, _ = self.run_sync([self.result("skipped", status, message)], "--fail-on-decision")
                self.assertEqual(code, 0)
        self.assertEqual(self.run_sync([self.result("create", "published", "")], "--fail-on-decision")[0], 0)
        self.assertEqual(self.run_sync([self.result("failed", None, "Invalid.")], "--fail-on-decision")[0], 1)

    def test_the_action_and_the_docs_offer_the_flag(self):
        action = (ROOT / "action" / "action.yml").read_text(encoding="utf-8")
        self.assertIn("fail-on-decision:", action)
        self.assertIn("--fail-on-decision", action)
        git = (ROOT / "docs" / "git-publishing.md").read_text(encoding="utf-8")
        self.assertRegex(git, r"\| `fail-on-decision` \| `false` \|")
        self.assertIn("--fail-on-decision", git)
        self.assertIn("inkcast resend ", git)
        self.assertIn("inkcast cancel ", git)


# ---- 9. the resend's ledger write goes through the publisher's store ------------------------------------------------


class FindingTheResendsLedgerWriteGoesThroughThePublishersStore(SyncHarness):
    def test_a_service_with_its_own_store_does_not_deadlock_on_the_resend(self):
        """``InkcastService.resend``'s ledger write runs inside ``resend_post``'s transaction, which the publisher's
        store holds. Through the service's own store it waited for that lock when the two were different connections
        (a harness gives them, and so could a split deployment): "database is locked" after the busy timeout, and no
        resend. ``GitSync._create`` writes its entry through the publisher's store for the same reason."""
        old_id = self.synced_then_capped()                                    # a Git-synced post with its entry
        api_store = self.open()
        api_store._conn.execute("PRAGMA busy_timeout = 300")                 # prove the lock in 0.3 s, not 30
        service = InkcastService(api_store, self.platforms, self.publisher, "https://inkcast.example.com")
        info = asyncio.run(service.resend(self.user, old_id))
        self.assertEqual((info.status, self.platform.sent), ("published", ["Hello"]))
        entry = self.entry()
        self.assertEqual((entry["post_id"], entry["status"], entry["url"]), (info.id, "published", info.url))


# ---- 10. repoint_entries moves only the person's own entries ----------------------------------------------------------


class FindingRepointMovesOnlyThePersonsEntries(SyncHarness):
    def test_another_persons_entry_naming_the_post_is_left_alone(self):
        """Ownership is checked in ``resend_post``; the ledger write that follows it filtered by the post id alone.
        Defence in depth: an entry of another person's that names the post (none should) stays theirs."""
        old_id = self.synced_then_capped()
        other = self.store.upsert_user("local:other", "other", None, None)["id"]
        self.store.put_entry(other, SOURCE, PATH, self.platform.id, status="unconfirmed", post_id=old_id, content_hash="h")
        info = asyncio.run(self.service.resend(self.user, old_id))
        mine, theirs = self.entry(), self.store.get_entry(other, SOURCE, PATH, self.platform.id)
        self.assertEqual((mine["post_id"], mine["status"]), (info.id, "published"))
        self.assertEqual((theirs["post_id"], theirs["status"]), (old_id, "unconfirmed"), "another person's entry was moved")
        self.assertEqual(self.store.repoint_entries(old_id, self.user, status="missing"), 0)
        self.assertEqual(self.store.repoint_entries(old_id, other, status="cancelled"), 1)


# ---- 11. the wording, and the cancelled event of a resent post ---------------------------------------------------------


class FindingTheWordingAndTheCancelledEventOfAResentPost(Harness):
    def test_a_resent_post_writes_post_cancelled_too_and_the_kpi_counts_it_once(self):
        """A resent post is 'cancelled' with only a ``post.resent`` event, so ``posts_cancelled`` and the per-status
        counts disagreed. With the event, the rollup (idempotent by event id) applies a replayed batch once."""
        old_id = self.unconfirmed()
        asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual([t for t, _, _ in self.events(old_id)], ["post.cancelled", "post.resent"])
        self.assertIn(("post.cancelled", self.user, {"platform": "findable", "reason": "resent"}), self.events(old_id))
        events = [e for e in self.store.poll("audit", limit=200) if e.subject == old_id]
        engine = RollupEngine(self.store)
        engine.handle(events)
        engine.handle(events)                                                 # the outbox replayed the batch
        day = bucket_start(events[0].at, "day")
        cancelled = self.store.read("posts_cancelled", "day", "user", self.user, day, day + DAY)
        self.assertEqual([(p.dims["platform"], p.value, p.n) for p in cancelled], [("findable", 1.0, 1)])
        self.assertEqual(self.store.post_counts().get("cancelled"), 1)

    def test_the_scope_the_table_the_comment_and_the_docs(self):
        self.assertIn("resend", SCOPES["posts:write"].lower())
        table = sync_module.__doc__[sync_module.__doc__.index("skipped"):sync_module.__doc__.index("failed ")]
        self.assertIn("decision", table)
        posts_source = (ROOT / "src/inkcast/libs/db/posts.py").read_text(encoding="utf-8")
        self.assertNotIn("set afresh by a later interruption", posts_source)
        self.assertIn("kept across a re-send interrupted", posts_source)
        git = (ROOT / "docs/git-publishing.md").read_text(encoding="utf-8")
        self.assertRegex(git, r"\| `posts:write` \|[^\n]*resend")
        from inkcast.libs.events import EVENT_TYPES
        self.assertIn("resent", EVENT_TYPES["post.cancelled"])


if __name__ == "__main__":
    unittest.main()
