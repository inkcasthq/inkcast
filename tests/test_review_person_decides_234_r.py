# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #442 (issue #234, the follow-ups of a person deciding on an unconfirmed post).

Each ``Finding`` class failed on the PR's head (47a531e) and passes with the fix committed beside it. Concurrency is
driven by hooks and asyncio events inside the platform's calls, never by sleeps, as ``test_person_decides`` does: another
worker is a second ``Publisher`` on its own connection, run as a task while the first worker's request is held."""

import asyncio
import contextlib
import io
import os
import time
import unittest
from unittest import mock

import httpx2

from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, _lost
from inkcast.libs.platforms import PlatformError, PublishResult
from inkcast.services.publishing.usecases import ServiceError

from test_person_decides import DAY, Findable, Harness, SyncHarness


class Lagging(Findable):
    """Findable whose request reaches the platform before its answer is held: ``during_publish(copy)`` runs once the
    copy exists. A lookup lists only the copies in ``listed`` (DEV's listing can lag a post; the code allows for it,
    "a lookup that lags the post"), and names the newest, as DEV names the one article it can tell is this post."""

    def __init__(self):
        super().__init__()
        self.listed: list[int] = []

    async def publish(self, creds, account, prepared, destination):
        self.sent.append(prepared.title)
        copy = len(self.sent)
        if self.during_publish:
            hook, self.during_publish = self.during_publish, None
            await hook(copy)
        if self.answer is not None:
            answer, self.answer = self.answer, None
            raise answer
        return PublishResult(url=f"https://findable/{copy}", remote_id=str(copy), status="published")

    async def find_existing(self, creds, account, prepared):
        self.lookups += 1
        newest = self.listed[-1] if self.listed else None
        answer = None if newest is None else PublishResult(url=f"https://findable/{newest}", remote_id=str(newest),
                                                           status="published")
        if self.meanwhile:
            hook, self.meanwhile = self.meanwhile, None
            await hook(prepared)
        return answer


class Editable(Findable):
    """Findable that edits posts in place, as DEV does (``update``)."""

    capabilities = frozenset({"find", "update"})

    async def update(self, creds, account, remote_id, prepared, destination):
        return PublishResult(url=f"https://findable/{remote_id}", remote_id=remote_id, status="published")


# ---- the note that an earlier send may have arrived too (#234 item 3) outlives every later write --------------------


class FindingRecoveryFoundOverUnconfirmedKeepsTheLostNote(Harness):
    platform_class = Lagging

    def test_a_found_answer_landing_after_an_earlier_sends_lost_answer_keeps_the_second_copy_flag(self):
        """A's send (attempt 1) is out; its copy reached the platform but is not listed yet. B's stuck pass is told
        "missing", re-queues the post and re-sends it (attempt 2); copy 2 is listed, B's answer is slow. C's stuck pass
        takes the post over at attempt 2 and finds copy 2. While C's answer is out, A's answer lands: a timeout after
        the request was sent, written over C's 'recovering' as 'unconfirmed' with the note that it may have arrived too
        (``update_post(beyond=)``). C's found answer is now written over 'unconfirmed' (#234 item 1) with error=None:
        the note is gone, B's success (the same copy C found) has nothing to keep, and a post the platform holds twice
        reads as any published post."""
        post_id = self.post("scheduled")
        b, c = self.replica(), self.replica()
        c_asking, release_c, release_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tasks = {}

        async def c_asks(prepared):                                           # inside C's lookup, which found copy 2
            c_asking.set()
            await release_c.wait()

        async def b_sends(copy):                                              # inside B's publish: copy 2 is listed
            self.platform.listed.append(copy)
            self.platform.meanwhile = c_asks
            tasks["c"] = asyncio.create_task(c.run_due(now=time.time() + 2 * (STUCK_AFTER + 1)))
            await release_b.wait()

        async def a_sends(copy):                                              # inside A's publish: copy 1 not listed
            self.platform.during_publish = b_sends
            tasks["b"] = asyncio.create_task(b.run_due(now=time.time() + STUCK_AFTER + 1))
            await c_asking.wait()
            raise PlatformError("Findable timed out after the request was sent.", uncertain=True)

        async def run():
            await self.publisher.run_due(now=time.time())                     # A's tick
            between = dict(self.store.get_post(post_id))
            release_c.set()
            await tasks["c"]
            after_c = dict(self.store.get_post(post_id))
            release_b.set()
            await tasks["b"]
            return between, after_c

        self.platform.during_publish = a_sends
        between, after_c = asyncio.run(run())
        lost = _lost(self.platform)
        self.assertEqual((between["status"], between["attempts"]), ("unconfirmed", 2))
        self.assertIn(lost, between["error"], "A's answer over C's pass did not say it may have arrived too")
        self.assertEqual(after_c["status"], "published")
        self.assertIn(lost, after_c["error"] or "", "C's found answer erased the second-copy flag")
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], self.platform.sent),
                         ("published", "https://findable/2", ["t", "t"]))
        self.assertIn(lost, post["error"] or "", "two copies on the platform, and the post reads as any published post")


class FindingGitSyncsFoundAnswerKeepsTheLostNote(SyncHarness):
    def test_the_syncs_check_records_the_post_and_keeps_the_second_copy_flag(self):
        """The sync asks the platform about an unconfirmed post and records it when found (``GitSync._resolve``), with
        error=None. A post 'unconfirmed' because an earlier send's answer landed over a re-send carries the note that
        a second copy may be there (the state ``test_person_decides_followups`` drives, its ``seen[1]``): the check
        erased it, and nothing else says so. ``record_success`` documents the note as one "only a person clears"."""
        self.platform.answer = PlatformError("Findable timed out after the request was sent.", uncertain=True)
        first = self.sync()
        self.assertEqual(first.status, "unconfirmed")
        lost = _lost(self.platform)
        self.store.update_post(first.post_id, error=f"Findable timed out after the request was sent.; {lost}")
        self.platform.sent.append("Hello")                                    # the platform has it (at least once)
        again = self.sync()
        post = self.store.get_post(first.post_id)
        self.assertEqual((again.status, post["status"]), ("published", "published"))
        self.assertIn(lost, post["error"] or "", "the sync's check erased the second-copy flag")


class FindingAnEditInPlaceKeepsTheLostNote(SyncHarness):
    platform_class = Editable

    def test_the_update_of_an_edited_file_keeps_the_second_copy_flag(self):
        """A re-send's success keeps the note (#234 item 3); the author then edits the file, and ``Publisher.update``
        wrote the update's own notes as the post's error, so the note went with the edit: an edit is no decision about
        a second copy. (It keeps the notes beside an ``_edited`` one, #392, but not otherwise.)"""
        first = self.sync()
        self.assertEqual(first.status, "published")
        lost = _lost(self.platform)
        self.store.update_post(first.post_id, error=lost)                     # as record_success leaves it (#234)
        edited = self.sync("Body, edited.")
        post = self.store.get_post(first.post_id)
        self.assertEqual((edited.action, post["status"]), ("update", "published"))
        self.assertIn(lost, post["error"] or "", "the edit in place erased the second-copy flag")


# ---- a resend while disconnected (#234 item 4) ---------------------------------------------------------------------


class Disconnecting(Findable):
    """Findable whose ``verify`` lets ``during_verify()`` run while the check is out (the person disconnects the
    platform in another tab, or the API's other replica does), then answers as the platform does: the key is good."""

    def __init__(self):
        super().__init__()
        self.during_verify = None

    async def verify(self, creds):
        if self.during_verify:
            hook, self.during_verify = self.during_verify, None
            hook()
        return await super().verify(creds)


class FindingADisconnectDuringTheCredentialsCheckStillRefusesTheResend(Harness):
    platform_class = Disconnecting

    def test_a_disconnect_while_verify_is_out_refuses_the_resend_before_anything_is_cancelled(self):
        """The credentials are read, then ``verify`` goes to the platform (a network round trip), then the old post is
        cancelled and the new one sent, which reads the credentials again: a disconnect while ``verify`` was out
        cancelled the old post and failed the new one ("Findable was disconnected."), the case #234 item 4 refuses."""
        old_id = self.unconfirmed()
        self.platform.during_verify = lambda: self.store.delete_credentials(self.user, self.platform.id)
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_connected"))
        self.assertEqual((self.status(old_id), len(self.posts()), self.platform.sent), ("unconfirmed", 1, []),
                         "the old post was cancelled for a new one that failed at once")


class FindingARetriedResendWhileDisconnectedNamesTheNewPost(Harness):
    def test_a_resend_retried_after_a_disconnect_is_told_the_post_the_first_one_made(self):
        """The first resend went through (its reply lost); the platform is disconnected before the client retries. The
        connection is checked before the post's status, so the retry is told "post ... was not resent and is still
        unconfirmed", which is false, and not the post the first resend made (#234 item 5): a client acting on it may
        publish by hand, a second copy."""
        old_id = self.unconfirmed()
        first = asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual(first.status, "published")
        self.store.delete_credentials(self.user, self.platform.id)
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertNotIn("still unconfirmed", caught.exception.message, "the reply says the resent post is unconfirmed")
        self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.extra),
                         (409, "not_resendable", {"post_id": first.id}))


# ---- the cap ends a post even when its last lookup can't be made (#234 item 2) ------------------------------------------


class FindingTheCapEndsAPostWhoseLastLookupCannotBeMade(Harness):
    def past_the_cap(self, user_id: str, title: str) -> str:
        prepared = self.platform.prepare({"title": title}, "Body.")
        post_id = self.store.create_post(group_id="g", user_id=user_id, platform=self.platform.id, title=title,
                                         status="recovering", run_at=time.time() - 3 * DAY, payload=prepared.to_dict())
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?, recovering_since = ? WHERE id = ?",
                         STUCK_AFTER + 1, time.time() - 2 * DAY, post_id)
        return post_id

    def test_credentials_that_cannot_be_read_neither_hold_the_post_nor_the_other_persons_posts(self):
        """The cap marked a post 'unconfirmed' without a lookup, so nothing could hold it past INKCAST_PUBLISH_RECOVERY_
        MAX_HOURS. It now asks first (#234 item 2), and ``confirm`` reads the stored credentials outside its guard: a
        person whose credentials can't be decrypted (INKCAST_SECRET_KEY changed: ``Sealer.unseal`` raises RuntimeError)
        raises out of ``recover``, every pass. The post stays 'recovering' for ever, and every other post the pass
        claimed after it (another person's, here) is never resolved either."""
        other_key = Store(self.path, "z" * 40)                                # the key the credentials were sealed with
        self.addCleanup(other_key.close)
        stale = self.store.upsert_user("local:stale-key", "stale-key", None, None)["id"]
        other_key.save_credentials(stale, self.platform.id, {"k": "v"}, {"id": "2", "name": "findable"})
        held = self.past_the_cap(stale, "held")
        theirs = self.past_the_cap(self.user, "theirs")
        self.platform.sent.append("theirs")                                   # the other person's post did arrive
        outcomes = self.tick()
        self.assertEqual({o.post_id: o.status for o in outcomes}, {held: "unconfirmed", theirs: "published"})
        self.assertEqual((self.status(held), self.status(theirs)), ("unconfirmed", "published"))
        self.assertIn("INKCAST_PUBLISH_RECOVERY_MAX_HOURS", self.store.get_post(held)["error"])
        self.assertEqual(self.tick(later=2 * DAY), [], "asked or sent again")


class FindingTheCapEndsAPostWhosePayloadCannotBeRead(Harness):
    def test_a_payload_from_a_newer_version_does_not_hold_the_post_past_the_cap(self):
        """The cap returned before the payload was read; ``recover`` now reads it first, for the cap's last lookup (#234
        item 2), and ``Prepared.from_dict`` refuses a key it doesn't know: a post a newer version sent, recovered by an
        older one after a rollback, raised out of ``recover`` on every pass and stayed 'recovering' for ever (on the
        parent commit it ended 'unconfirmed')."""
        payload = self.platform.prepare({"title": "newer"}, "Body.").to_dict()
        payload["added_later"] = True
        post_id = self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title="newer",
                                         status="recovering", run_at=time.time() - 3 * DAY, payload=payload)
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?, recovering_since = ? WHERE id = ?",
                         STUCK_AFTER + 1, time.time() - 2 * DAY, post_id)
        (outcome,) = self.tick()
        self.assertEqual((outcome.status, self.status(post_id), self.platform.lookups), ("unconfirmed", "unconfirmed", 0))
        self.assertIn("could not be asked whether it arrived for 24 hours", self.store.get_post(post_id)["error"])


# ---- the CLI's post id is one path segment (#234 item 8) ---------------------------------------------------------------


class FindingTheCliSendsThePostIdAsOnePathSegment(unittest.TestCase):
    def test_a_post_id_holding_a_slash_or_a_hash_cannot_turn_a_resend_into_a_cancel(self):
        """``inkcast resend <post-id>`` put the id into the path as given: "pst_x/cancel#" made the request
        ``POST /api/v1/posts/pst_x/cancel`` (the rest is a fragment), so a resend cancelled the post, and "../tokens"
        left the posts routes. A workflow that passes a post id from its input (a dispatch input, a comment) is
        driven by whoever writes that input. The id is now one escaped segment: the API answers 404 for it."""
        from inkcast import cli

        paths: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            paths.append(request.url.raw_path.decode())
            return httpx2.Response(404, json={"error": "not_found", "message": "No post."})

        def client(base_url: str, token: str) -> httpx2.Client:
            return httpx2.Client(base_url=base_url, transport=httpx2.MockTransport(handler))

        err = io.StringIO()
        with mock.patch.dict(os.environ, {"INKCAST_URL": "https://inkcast.example.com", "INKCAST_TOKEN": "ink_pat_test"}), \
                mock.patch.object(cli, "_client", client), contextlib.redirect_stderr(err):
            codes = [cli.post_main("resend", [post_id]) for post_id in ("pst_x/cancel#", "../tokens", "pst_x?y")]
        self.assertEqual(codes, [1, 1, 1])
        self.assertEqual(paths, ["/api/v1/posts/pst_x%2Fcancel%23/resend", "/api/v1/posts/..%2Ftokens/resend",
                                 "/api/v1/posts/pst_x%3Fy/resend"], "the post id left its path segment")
