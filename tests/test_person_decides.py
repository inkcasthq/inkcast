# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #226: an 'unconfirmed' post is a person's to decide, by cancelling it or resending it; Git sync never creates
it again.

After #216's cap marked a Git-synced post 'unconfirmed', the next push could create and send it again if the platform
said it was missing (a day or more late, triggered by whatever was pushed), and a person could not cancel an
'unconfirmed' post: #216's "never sent late without a person deciding" did not hold. Now:

* a person can cancel an 'unconfirmed' post (MCP, REST), and the reply says it may already be on the platform;
* a person can resend one (``inkcast_resend``, ``POST /api/v1/posts/{id}/resend``): exactly one new post, sent now,
  the old one cancelled, and a ``post.resent`` event recording who decided;
* Git sync never creates a post again whose entry points at an 'unconfirmed' post (capped or otherwise): it reports
  it as needing that decision, and a post the platform does have is recorded;
* from the review of #221: the cap's clock survives a re-entry (item 2); recovery's and the sender's late answers are
  fenced by the attempt count (items 3 and 4); the plug-in-missing write and the cap's count yield to a cancel (items
  5 and 6); the wording (item 7).

Each ``Finding`` test failed on main before the fix committed with it; the ``Held`` tests pin what stays. Concurrency
is driven by hooks and barriers inside the platform's calls, never by sleeps."""

import asyncio
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path

import httpx2

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.contracts.openapi import openapi_document
from inkcast.libs.db import Store
from inkcast.libs.db.ledger import CLAIM_TTL
from inkcast.libs.events import EVENT_TYPES
from inkcast.libs.observability import Metrics, route_label
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.services.publishing.mcp_tools import render_cancel, render_resend
from inkcast.services.publishing.sync import GitSync
from inkcast.services.publishing.usecases import InkcastService, ServiceError

ROOT = Path(__file__).resolve().parents[1]
HOUR, DAY = 3600, 86400
SOURCE = "github.com/acme/blog"
PATH = "posts/p.md"


class Findable(Platform):
    """A platform that can be checked (``find``). ``refuse`` makes every lookup fail for a reason that will pass (a
    429); otherwise it finds what reached it (``sent``). ``answer`` makes the next publish raise that error instead
    of delivering. ``meanwhile(prepared)`` (an async hook) runs inside each lookup while set, after the platform has
    answered and before the caller sees the answer; ``during_publish(prepared)`` runs inside the next publish, while
    the request is out: each unsets itself first, so a nested call is a plain one."""

    id, name, default_api_url, credential_fields = "findable", "Findable", "https://findable.invalid", []
    capabilities = frozenset({"find"})

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sent: list[str] = []
        self.lookups = 0
        self.refuse = False
        self.answer: PlatformError | None = None
        self.meanwhile = None
        self.during_publish = None

    async def verify(self, creds):
        return Account(id="1", name="findable")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        if self.during_publish:
            hook, self.during_publish = self.during_publish, None
            await hook(prepared)
        if self.answer is not None:
            answer, self.answer = self.answer, None
            raise answer
        self.sent.append(prepared.title)
        return PublishResult(url=f"https://findable/{len(self.sent)}", remote_id=str(len(self.sent)), status="published")

    async def find_existing(self, creds, account, prepared):
        self.lookups += 1
        if self.refuse:
            raise PlatformError("Findable rate limit hit; try again in 15 minutes.", retryable=True, retry_after=900)
        answer = (PublishResult(url="https://findable/found", remote_id="found", status="published")
                  if prepared.title in self.sent else None)
        if self.meanwhile:
            hook, self.meanwhile = self.meanwhile, None
            await hook(prepared)
        return answer


class Blind(Findable):
    """The same platform without ``find`` (like Medium): an uncertain send can't be checked automatically."""

    id, name, default_api_url = "blind", "Blind", "https://blind.invalid"
    capabilities = frozenset()


class Harness(unittest.TestCase):
    """One database file, opened by the scheduler under test (``publisher``) and, where a test needs them, by other
    processes on their own connections (another replica, the API)."""

    platform_class: type[Findable] = Findable

    def setUp(self):
        self.path = tempfile.mkdtemp() + "/inkcast.db"
        self.store = self.open()
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = self.platform_class()
        self.platforms = {self.platform.id: self.platform}
        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
        self.publisher = Publisher(self.store, self.platforms)
        self.service = InkcastService(self.store, self.platforms, self.publisher, "https://inkcast.example.com")
        quiet = logging.getLogger("inkcast.libs.pipeline")
        level, quiet.level = quiet.level, logging.CRITICAL   # recovery's warnings are expected; keep the run readable
        self.addCleanup(setattr, quiet, "level", level)

    def open(self) -> Store:
        store = Store(self.path, "k" * 40)
        self.addCleanup(store.close)
        return store

    def replica(self) -> Publisher:
        """Another process on the same database, with its own connection."""
        return Publisher(self.open(), self.platforms)

    def post(self, status: str, title: str = "t", **fields) -> str:
        prepared = self.platform.prepare({"title": title}, "Body.")
        post_id = self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title=title,
                                         status=status, run_at=time.time() - 1, payload=prepared.to_dict())
        if fields:
            self.store.update_post(post_id, **fields)
        return post_id

    def unconfirmed(self, title: str = "t") -> str:
        """A post whose send got an uncertain answer (a timeout after the request was sent): the usual way."""
        return self.post("unconfirmed", title, error="Findable timed out after the request was sent.")

    def capped(self, title: str = "t") -> str:
        """A post interrupted mid-send that the platform refused to be asked about for a day: unconfirmed by the cap."""
        post_id = self.post("publishing", title)
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        self.platform.refuse = True
        self.tick()
        self.assertEqual(self.status(post_id), "recovering")
        self.tick(later=DAY + 1)
        self.assertEqual(self.status(post_id), "unconfirmed")
        self.platform.refuse = False
        return post_id

    def tick(self, later: float = 0.0, publisher: Publisher | None = None):
        return asyncio.run((publisher or self.publisher).run_due(now=time.time() + later))

    def status(self, post_id: str) -> str:
        return self.store.get_post(post_id)["status"]

    def events(self, post_id: str) -> list[tuple[str, str | None, dict]]:
        return [(e.type, e.user_id, e.attrs) for e in self.store.poll("audit", limit=200) if e.subject == post_id]

    def posts(self) -> list[dict]:
        return [dict(r) for r in self.store._all("SELECT * FROM posts ORDER BY rowid")]


class SyncHarness(Harness):
    def setUp(self):
        super().setUp()
        self.git = GitSync(self.store, self.platforms, self.publisher)

    def markdown(self, body: str = "Body.") -> str:
        return f"---\ntitle: Hello\nplatforms: [{self.platform.id}]\n---\n\n{body}\n"

    def sync(self, body: str = "Body."):
        return asyncio.run(self.sync_async(body))

    async def sync_async(self, body: str = "Body."):
        """One push of the file, from inside a hook (the loop is already running)."""
        req = SyncRequest(source=SOURCE, files=[SyncFile(path=PATH, markdown=self.markdown(body))])
        (result,) = (await self.git.sync(self.user, req)).results
        return result

    def entry(self) -> dict:
        return self.store.get_entry(self.user, SOURCE, PATH, self.platform.id)

    def synced_then_capped(self) -> str:
        """A Git-synced post whose sync died mid-send, recovered for a day with the platform refusing the lookup, and
        was unconfirmed by the cap: the case the issue is about. The request never reached the platform."""
        class Died(BaseException):
            """The process stopped right here: nothing after this line runs."""

        async def died(prepared):
            raise Died()

        self.platform.during_publish = died
        with self.assertRaises(Died):
            self.sync()
        (post,) = self.posts()
        self.store._exec("UPDATE ledger SET synced_at = synced_at - ?", CLAIM_TTL + 1)
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?", STUCK_AFTER + 1)
        self.platform.refuse = True
        self.tick()
        self.tick(later=DAY + 1)
        self.platform.refuse = False
        self.assertEqual((self.status(post["id"]), self.platform.sent), ("unconfirmed", []))
        return post["id"]


# ---- a person can cancel an unconfirmed post ---------------------------------------------------------------------


class FindingAnUnconfirmedPostCanBeCancelled(Harness):
    def test_the_cancel_is_accepted_and_says_the_post_may_be_on_the_platform(self):
        for how in (self.capped, self.unconfirmed):
            with self.subTest(how=how.__name__):
                post_id = how()
                title, was = self.publisher.cancel_pending(self.user, post_id)
                self.assertEqual((title, was), ("t", "unconfirmed"))
                post = self.store.get_post(post_id)
                self.assertEqual(post["status"], "cancelled")
                self.assertIn("may already be on Findable", post["error"])
                self.assertIn("remove it there if it is", post["error"])
                self.assertIn(("post.cancelled", self.user, {"platform": "findable"}), self.events(post_id))
                self.platform.refuse = False
                self.tick(later=2 * DAY)                                      # nothing ever sends it
                self.assertEqual((self.status(post_id), self.platform.sent), ("cancelled", []))

    def test_the_rest_reply_and_the_mcp_reply_say_so_and_a_scheduled_cancel_does_not(self):
        post_id = self.unconfirmed()
        info, was = self.service.cancel_from(self.user, post_id)              # the MCP tool
        self.assertEqual((info.status, was), ("cancelled", "unconfirmed"))
        self.assertIn("may already be on Findable", info.error)
        reply = render_cancel(info, was)
        self.assertIn(f"Cancelled “t” (post `{post_id}`)", reply)
        self.assertIn("may already be on Findable", reply)
        self.assertIn("may already be on Findable", self.service.cancel(self.user, self.unconfirmed("u")).error)   # REST
        with self.assertRaises(ServiceError) as caught:                       # a second cancel: nothing to do
            self.service.cancel(self.user, post_id)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_cancellable"))
        self.assertIn("is cancelled; only scheduled, recovering or unconfirmed posts can be cancelled",
                      caught.exception.message)
        scheduled = self.post("scheduled", "s", error="retrying: Findable is unreachable.")
        info, was = self.service.cancel_from(self.user, scheduled)
        self.assertEqual(was, "scheduled")
        self.assertEqual(render_cancel(info, was), f"Cancelled “s” (post `{scheduled}`).")   # no note: it never went out


# ---- a person can resend an unconfirmed post ---------------------------------------------------------------------


class FindingAnUnconfirmedPostCanBeResent(Harness):
    def test_a_resend_makes_exactly_one_new_post_sends_it_now_and_records_who_decided(self):
        for how in (self.capped, self.unconfirmed):
            with self.subTest(how=how.__name__):
                old_id = how(how.__name__)
                before = list(self.platform.sent)
                info = asyncio.run(self.service.resend(self.user, old_id))
                self.assertNotEqual(info.id, old_id)
                self.assertEqual((info.status, info.url, info.title), ("published", f"https://findable/{len(before) + 1}",
                                                                      how.__name__))
                self.assertEqual(self.platform.sent, before + [how.__name__], "sent once")
                old, new = self.store.get_post(old_id), self.store.get_post(info.id)
                self.assertEqual((old["status"], new["status"]), ("cancelled", "published"))
                self.assertIn(f"Resent by the person as post {info.id}", old["error"])
                self.assertIn("may already be on Findable", old["error"])
                self.assertEqual((new["group_id"], new["platform"], new["payload"]), (old["group_id"], "findable", old["payload"]))
                self.assertIsNone(new["recovering_since"], "a person's decision starts a fresh post")
                self.assertIn(("post.resent", self.user, {"platform": "findable", "new_post_id": info.id}), self.events(old_id))
                self.assertIn(("post.published", self.user, {"platform": "findable", "status": "published", "scheduled": False,
                                                            "attempts": 1, "lag_seconds": None}), self.events(info.id))
                self.assertIn("post.resent", EVENT_TYPES)
                with self.assertRaises(ServiceError) as caught:               # a second resend: the first decided
                    asyncio.run(self.service.resend(self.user, old_id))
                self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_resendable"))
                self.assertIn("is cancelled; only unconfirmed posts can be resent", caught.exception.message)
                self.assertEqual(len([p for p in self.posts() if p["title"] == how.__name__]), 2)

    def test_two_resends_at_once_make_one_post(self):
        """The API (two processes) resends the same post twice at once: the second lands while the first's send is out
        at the platform. One new post, one send."""
        old_id = self.unconfirmed()
        api = self.replica()
        refused: list[ServiceError] = []

        async def the_second_resend(prepared):
            other = InkcastService(api.store, self.platforms, api, "https://inkcast.example.com")
            try:
                await other.resend(self.user, old_id)
            except ServiceError as exc:
                refused.append(exc)

        self.platform.during_publish = the_second_resend
        info = asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual(info.status, "published")
        self.assertEqual([e.code for e in refused], ["not_resendable"])
        self.assertEqual((self.platform.sent, [p["status"] for p in self.posts()]), (["t"], ["cancelled", "published"]))
        self.assertEqual(self.store.resend_post(old_id, self.user, new_id="pst_never", note="n"), False)
        self.assertIsNone(self.store.get_post("pst_never"))

    def test_only_an_unconfirmed_post_of_the_persons_can_be_resent(self):
        for status in ("scheduled", "publishing", "recovering", "published", "failed", "cancelled"):
            with self.subTest(status=status):
                post_id = self.post(status, status)
                with self.assertRaises(ServiceError) as caught:
                    asyncio.run(self.service.resend(self.user, post_id))
                self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_resendable"))
                self.assertIn(f"is {status}", caught.exception.message)
                self.assertEqual(self.status(post_id), status)
        post_id = self.unconfirmed()
        for who, which in (("someone-else", post_id), (self.user, "pst_nope")):    # another person's; no such post
            with self.subTest(who=who, which=which):
                with self.assertRaises(ServiceError) as caught:
                    asyncio.run(self.service.resend(who, which))
                self.assertEqual((caught.exception.status, caught.exception.code), (404, "not_found"))
        self.assertEqual((self.status(post_id), self.platform.sent), ("unconfirmed", []))

    def test_a_resend_that_fails_or_is_uncertain_in_its_turn_says_so(self):
        failed = self.unconfirmed("f")
        self.platform.answer = PlatformError("Findable refused the post (400).")
        info = asyncio.run(self.service.resend(self.user, failed))
        self.assertEqual((info.status, info.error), ("failed", "Findable refused the post (400)."))
        self.assertEqual(self.status(failed), "cancelled")
        uncertain = self.unconfirmed("u")
        self.platform.answer = PlatformError("Findable timed out after the request was sent.", uncertain=True)
        info = asyncio.run(self.service.resend(self.user, uncertain))
        self.assertEqual(info.status, "unconfirmed")                          # and the person decides again
        self.assertEqual((self.status(uncertain), self.platform.sent), ("cancelled", []))
        rate_limited = self.unconfirmed("r")
        self.platform.answer = PlatformError("Findable rate limit hit.", retryable=True, retry_after=60)
        info = asyncio.run(self.service.resend(self.user, rate_limited))
        self.assertEqual(info.status, "failed", "a person is there to try again: nothing is queued on its own")
        self.assertEqual(self.tick(later=DAY), [])
        reply = render_resend(info, rate_limited, self.platforms)
        self.assertIn(f"Resent “r” (post `{rate_limited}`) as post `{info.id}`: FAILED — Findable rate limit hit.", reply)
        self.assertIn("may already be on Findable", reply)
        ok = render_resend(asyncio.run(self.service.resend(self.user, self.unconfirmed("ok"))), "pst_old", self.platforms)
        self.assertIn(": published → https://findable/1", ok)


class FindingAResendMovesTheGitSyncedFileToTheNewPost(SyncHarness):
    def test_the_next_sync_follows_the_new_post_and_sends_nothing(self):
        old_id = self.synced_then_capped()
        info = asyncio.run(self.service.resend(self.user, old_id))
        entry = self.entry()
        self.assertEqual((entry["post_id"], entry["status"], entry["url"], entry["remote_id"]),
                         (info.id, "published", "https://findable/1", "1"))
        again = self.sync()
        self.assertEqual((again.action, again.status, again.post_id, again.url), ("unchanged", "published", info.id, info.url))
        self.assertEqual(self.platform.sent, ["Hello"])
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "published"])

    def test_the_file_follows_the_new_post_before_anything_is_sent(self):
        """A sync that runs while the resend's request is out finds the entry on the new post, 'publishing', and
        leaves it to the scheduler: never a third post."""
        old_id = self.synced_then_capped()
        seen = []

        async def a_push_meanwhile(prepared):
            seen.append(await self.sync_async())

        self.platform.during_publish = a_push_meanwhile
        info = asyncio.run(self.service.resend(self.user, old_id))
        (mid,) = seen
        self.assertEqual((mid.action, mid.status, mid.post_id), ("unchanged", "publishing", info.id))
        self.assertEqual((self.platform.sent, len(self.posts())), (["Hello"], 2))


# ---- Git sync never creates an unconfirmed post again ------------------------------------------------------------


class FindingGitSyncNeverRecreatesAnUnconfirmedPost(SyncHarness):
    """The issue's case: the cap left a Git-synced post 'unconfirmed'; the platform, asked at the next push, says it is
    missing (its request never arrived). The sync used to mark the post 'failed' and create it again, a day late, on
    whatever was pushed. Now the person decides: the sync reports it, and never creates it."""

    def test_the_next_pushes_report_the_decision_and_send_nothing(self):
        post_id = self.synced_then_capped()
        for body in ("Body.", "Body.", "Better body."):                       # unchanged twice, then an edit
            with self.subTest(body=body):
                result = self.sync(body)
                self.assertEqual((result.action, result.status, result.post_id), ("skipped", "unconfirmed", post_id))
                self.assertIn("Findable says it isn't there", result.message)
                self.assertIn(f"resend post {post_id} (inkcast_resend, or POST /api/v1/posts/{post_id}/resend)", result.message)
                self.assertIn(f"cancel it (inkcast_cancel, or POST /api/v1/posts/{post_id}/cancel)", result.message)
                self.assertIn("remove the first copy on Findable", result.message)
                self.assertEqual((self.status(post_id), self.platform.sent, len(self.posts())), ("unconfirmed", [], 1))
                self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("unconfirmed", post_id))
        self.assertEqual(self.tick(later=2 * DAY), [], "the scheduler sent it")

    def test_a_cancel_then_lets_an_edited_file_be_a_new_post_and_an_unchanged_one_stay_cancelled(self):
        post_id = self.synced_then_capped()
        self.publisher.cancel(self.user, post_id)
        same = self.sync()
        self.assertEqual((same.action, same.status, same.post_id), ("unchanged", "cancelled", post_id))
        self.assertEqual(self.platform.sent, [])
        edited = self.sync("Better body.")
        self.assertEqual((edited.action, edited.status), ("create", "published"))
        self.assertNotEqual(edited.post_id, post_id)
        self.assertEqual((self.platform.sent, self.status(post_id)), (["Hello"], "cancelled"))

    def test_an_uncertain_now_post_is_never_created_again_either(self):
        """"Capped or otherwise": a sync's own send got an uncertain answer. The next sync asked the platform and, told
        "missing", created the post again; now that too is the person's decision."""
        self.platform.answer = PlatformError("Findable timed out after the request was sent.", uncertain=True)
        first = self.sync()
        self.assertEqual((first.action, first.status), ("create", "unconfirmed"))
        again = self.sync()
        self.assertEqual((again.action, again.status, again.post_id), ("skipped", "unconfirmed", first.post_id))
        self.assertIn("resend post", again.message)
        self.assertEqual((self.platform.sent, len(self.posts())), ([], 1), "created again")
        # The person resends it: one post goes out, and the file follows it.
        info = asyncio.run(self.service.resend(self.user, first.post_id))
        self.assertEqual((info.status, self.platform.sent), ("published", ["Hello"]))
        self.assertEqual((self.sync().action, self.sync().status), ("unchanged", "published"))

    def test_a_post_the_platform_does_have_is_still_recorded(self):
        """The lookup still resolves what it can: the first send did arrive after all. Nothing is sent."""
        post_id = self.synced_then_capped()
        self.platform.sent.append("Hello")                                    # it had reached Findable
        found = self.sync()
        self.assertEqual((found.action, found.status, found.url), ("unchanged", "published", "https://findable/found"))
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], post["remote_id"]), ("published", "https://findable/found", "found"))
        self.assertEqual(self.entry()["status"], "published")

    def test_a_found_answer_does_not_overwrite_a_decision_made_meanwhile(self):
        """The person cancels (or resends) while the sync's lookup is out: the cancel holds, and the entry follows it."""
        post_id = self.synced_then_capped()
        self.platform.sent.append("Hello")
        api = self.replica()

        async def the_person_cancels(prepared):
            api.cancel(self.user, post_id)

        self.platform.meanwhile = the_person_cancels
        result = self.sync()
        self.assertEqual((result.action, result.status), ("unchanged", "cancelled"))
        self.assertEqual((self.status(post_id), self.entry()["status"]), ("cancelled", "cancelled"))


class FindingABlindPlatformSaysTheSame(SyncHarness):
    platform_class = Blind

    def test_a_post_that_cannot_be_checked_is_the_persons_to_decide(self):
        post_id = self.synced_then_capped()
        result = self.sync()
        self.assertEqual((result.action, result.status, result.post_id), ("skipped", "unconfirmed", post_id))
        self.assertIn("Blind can't be checked automatically", result.message)
        self.assertIn(f"resend post {post_id}", result.message)
        self.assertNotIn("remove this file's ledger entry", result.message)
        self.assertEqual((self.platform.sent, len(self.posts())), ([], 1))


class HeldAnUnconfirmedEntryWithoutAPostStillResolvesItself(SyncHarness):
    def test_an_entry_from_before_posts_were_recorded_is_created_when_missing(self):
        """Nothing to decide on: no post row. The lookup decides, as before."""
        self.store.put_entry(self.user, SOURCE, PATH, self.platform.id, status="unconfirmed", content_hash="h")
        result = self.sync()
        self.assertEqual((result.action, result.status, self.platform.sent), ("create", "published", ["Hello"]))


# ---- the review of #221: item 2, the cap's clock survives a re-entry ---------------------------------------------


class FindingTheCapCountsFromTheFirstInterruption(Harness):
    def test_a_post_interrupted_twice_is_not_sent_two_days_after_the_first_time(self):
        """Interrupted; the platform refuses the lookup for 23 hours, then says "missing": re-queued and sent, and that
        send is interrupted too. The second recovery started a fresh 24 hours, so the post could go out 47 hours after
        its first interruption with nobody deciding. Now the clock is the first interruption's."""
        class Died(BaseException):
            """The process stopped right here: nothing after this line runs."""

        async def died(prepared):
            raise Died()

        post_id = self.post("publishing")
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        self.platform.refuse = True
        self.tick()                                                           # recovering since now
        self.platform.refuse = False
        self.platform.during_publish = died
        with self.assertRaises(Died):
            self.tick(later=23 * HOUR)                                        # missing → re-queued → sent → died
        self.assertEqual(self.status(post_id), "publishing")
        self.store._exec("UPDATE posts SET updated_at = ? WHERE id = ?", time.time() + 23 * HOUR, post_id)
        self.platform.refuse = True
        self.tick(later=23 * HOUR + STUCK_AFTER + 1)                          # taken again, refused again
        self.assertEqual(self.status(post_id), "recovering")
        self.platform.refuse = False                                          # it would say "missing" now
        self.tick(later=DAY + STUCK_AFTER + 2)                                # a day after the first interruption
        self.assertEqual((self.status(post_id), self.platform.sent), ("unconfirmed", []), "sent on its own")
        self.assertEqual(self.tick(later=2 * DAY), [])


# ---- items 3 and 4: late answers are fenced by the attempt count -------------------------------------------------


class FindingAStaleRecoveryAnswerDoesNotReQueueAReSentPost(Harness):
    def test_a_lookup_that_outlasts_two_stuck_passes_cannot_send_the_post_a_second_time(self):
        """Worker A's lookup is out. Worker B's stuck pass takes the post, is told "missing", re-queues and sends it;
        that send is interrupted; worker C's stuck pass takes it again. A's old "missing" then lands on a post that is
        'recovering' once more, and re-queued it: a second send of a post that B's request may have delivered."""
        class Died(BaseException):
            """The process stopped right here: nothing after this line runs."""

        async def died(prepared):
            self.platform.sent.append(prepared.title)                         # the request reached the platform
            raise Died()

        post_id = self.post("publishing")
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        self.platform.refuse = True
        self.tick()                                                           # A: recovering, refused
        self.platform.refuse = False
        b, c = self.replica(), self.replica()
        seen = []

        async def while_a_waits(prepared):
            self.platform.during_publish = died
            try:
                await b.run_due(now=time.time() + 2 * (STUCK_AFTER + 1))      # B: missing → re-queued → sent → died
            except Died:
                pass
            seen.append((self.status(post_id), list(self.platform.sent)))
            self.platform.refuse = True
            await c.run_due(now=time.time() + 3 * (STUCK_AFTER + 1))          # C: recovering again, refused
            self.platform.refuse = False
            seen.append(self.status(post_id))

        self.platform.meanwhile = while_a_waits
        self.tick(later=STUCK_AFTER + 1)                                      # A's pass: its answer lands last
        self.assertEqual(seen, [("publishing", ["t"]), "recovering"])
        self.assertEqual((self.status(post_id), self.platform.sent), ("recovering", ["t"]), "sent twice")
        self.tick(later=4 * (STUCK_AFTER + 1))                                # C's next pass: found, so published
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], self.platform.sent), ("published", "https://findable/found", ["t"]))


class FindingTheSendersLateFailureDoesNotQueueAThirdSend(Harness):
    def test_a_retryable_failure_landing_while_another_worker_re_sends_is_not_written(self):
        """Worker A's send is out. Worker B's stuck pass takes the post, is told "missing", re-queues it and starts
        sending it; B's request is out when A's answer, a retryable failure, lands. Written over B's 'publishing' (the
        same status A held), it queued a retry; a tick after the delay (60 s, before B's send looks stuck) sent the
        post a third time while B's request was still out. A's answer is written only at A's own attempt count."""
        post_id = self.post("scheduled")
        b, c = self.replica(), self.replica()
        b_out, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def b_sends(prepared):                                          # inside B's publish: hold its request
            b_out.set()
            await release.wait()

        async def while_a_waits(prepared):                                    # inside A's publish
            self.platform.during_publish = b_sends
            task = asyncio.create_task(b.run_due(now=time.time() + STUCK_AFTER + 1))   # B: missing → re-queued → sending
            await b_out.wait()
            seen.append(("b out", self.status(post_id), self.store.get_post(post_id)["attempts"]))
            self.tasks.append(task)
            raise PlatformError("Findable is unreachable.", retryable=True)   # A's answer

        async def run():
            self.tasks = []
            await self.publisher.run_due(now=time.time())                     # A's tick
            seen.append(("a done", self.status(post_id)))
            await c.run_due(now=time.time() + 120)                            # after the retry delay (60 s)
            seen.append(("c done", self.status(post_id), list(self.platform.sent)))
            release.set()
            await asyncio.gather(*self.tasks)

        self.platform.during_publish = while_a_waits
        asyncio.run(run())
        self.assertEqual(seen[0], ("b out", "publishing", 2))
        self.assertEqual(seen[1], ("a done", "publishing"), "A's late answer was written over B's send")
        self.assertEqual(seen[2], ("c done", "publishing", []), "sent a third time")
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], self.platform.sent), ("published", "https://findable/1", ["t"]))

    def test_a_definite_failure_landing_while_another_worker_re_sends_is_not_written(self):
        post_id = self.post("scheduled")
        b = self.replica()
        b_out, release = asyncio.Event(), asyncio.Event()

        async def b_sends(prepared):
            b_out.set()
            await release.wait()

        async def while_a_waits(prepared):
            self.platform.during_publish = b_sends
            self.tasks.append(asyncio.create_task(b.run_due(now=time.time() + STUCK_AFTER + 1)))
            await b_out.wait()
            raise PlatformError("Findable refused the post (400).")

        async def run():
            self.tasks = []
            await self.publisher.run_due(now=time.time())
            status = self.status(post_id)
            release.set()
            await asyncio.gather(*self.tasks)
            return status

        self.platform.during_publish = while_a_waits
        self.assertEqual(asyncio.run(run()), "publishing", "A's 'failed' was written over B's send, for Git sync to create again")
        self.assertEqual((self.status(post_id), self.platform.sent), ("published", ["t"]))


class HeldTheSendersOwnAnswerIsStillWritten(Harness):
    def test_a_failure_with_nobody_else_holding_the_post_is_recorded_as_before(self):
        retry = self.post("scheduled", "r")
        self.platform.answer = PlatformError("Findable is unreachable.", retryable=True)
        self.tick()
        post = self.store.get_post(retry)
        self.assertEqual((post["status"], post["attempts"]), ("scheduled", 1))
        failed = self.post("scheduled", "f")
        self.platform.answer = PlatformError("Findable refused the post (400).")
        self.tick()
        self.assertEqual(self.status(failed), "failed")
        now_post = self.post("publishing", "n")                               # a "now" post: its row reads 0 attempts
        self.platform.answer = PlatformError("Findable refused the post (400).")
        prepared = Prepared.from_dict(json.loads(self.store.get_post(now_post)["payload"]))
        asyncio.run(self.publisher._send(now_post, self.user, self.platform, prepared, attempts=1))
        self.assertEqual(self.status(now_post), "failed")


# ---- items 5 and 6: a cancel that comes first holds against the plug-in-missing write and the cap ------------------


class FindingACancelHoldsAgainstTheCapAndThePluginMissingWrite(Harness):
    def two_recovering(self, second_platform: str) -> tuple[str, str]:
        """Two posts a stuck pass takes together: the first on Findable (its lookup is where the person acts), the second
        on ``second_platform``."""
        first = self.post("recovering", "first")
        prepared = self.platform.prepare({"title": "second"}, "Body.")
        second = self.store.create_post(group_id="g", user_id=self.user, platform=second_platform, title="second",
                                        status="recovering", run_at=time.time() - 1, payload=prepared.to_dict())
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?, recovering_since = ?", STUCK_AFTER + 1,
                         time.time() - 2 * DAY)                              # both past the cap, both stuck
        return first, second

    def test_the_cap_neither_counts_nor_logs_a_post_the_person_cancelled_a_moment_before(self):
        first, second = self.two_recovering(self.platform.id)
        self.store._exec("UPDATE posts SET recovering_since = ? WHERE id = ?", time.time() - HOUR, first)   # not capped
        self.publisher.metrics = Metrics()
        self.publisher.metrics.counter("inkcast_publish_total", "")
        api = self.replica()

        async def the_person_cancels(prepared):
            api.cancel(self.user, second)

        self.platform.meanwhile = the_person_cancels
        logging.getLogger("inkcast.libs.pipeline").level = logging.NOTSET
        with self.assertLogs("inkcast.libs.pipeline", "WARNING") as logs:
            outcomes = self.tick()
        self.assertEqual([(o.post_id, o.status) for o in outcomes][:2], [(first, "scheduled"), (second, "cancelled")])
        self.assertEqual(self.status(second), "cancelled")
        self.assertNotIn((("platform", "findable"), ("status", "unconfirmed")),
                         self.publisher.metrics.snapshot()["inkcast_publish_total"], "the alert fires for a cancelled post")
        capped = [r for r in logs.records if getattr(r, "event", "") == "publish.recovery.capped"]
        self.assertEqual(capped, [], "logged as unconfirmed")
        self.assertEqual([t for t, _, _ in self.events(second)], ["post.cancelled"])

    def test_the_plugin_missing_write_yields_to_the_cancel(self):
        first, second = self.two_recovering("gone")                           # no plug-in named "gone" is installed
        api = self.replica()

        async def the_person_cancels(prepared):
            api.cancel(self.user, second)

        self.platform.meanwhile = the_person_cancels
        self.store._exec("UPDATE posts SET recovering_since = ? WHERE id = ?", time.time() - HOUR, first)
        outcomes = self.tick()
        self.assertEqual([(o.post_id, o.status) for o in outcomes][:2], [(first, "scheduled"), (second, "cancelled")])
        self.assertEqual(self.status(second), "cancelled")
        self.assertEqual([t for t, _, _ in self.events(second)], ["post.cancelled"], "failed over the cancel")
        self.store._exec("UPDATE posts SET status = 'recovering', updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1,
                         second)                                              # left alone, it is 'failed' as before
        self.tick()
        self.assertEqual(self.status(second), "failed")


# ---- item 7: the wording -----------------------------------------------------------------------------------------


class FindingTheWording(SyncHarness):
    def test_the_recovering_sync_message_says_it_ends_and_warns_about_a_second_copy(self):
        post_id = self.post("publishing")
        self.store.put_entry(self.user, SOURCE, PATH, self.platform.id, status="publishing", post_id=post_id,
                             content_hash="h")
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        self.platform.refuse = True
        self.tick()
        result = self.sync("Better body.")
        self.assertEqual((result.action, result.status), ("skipped", "recovering"))
        self.assertNotIn("keeps asking", result.message)
        self.assertIn("INKCAST_PUBLISH_RECOVERY_MAX_HOURS", result.message)
        self.assertIn("remove the first copy on Findable", result.message)

    def test_the_contracts_and_the_docs_name_the_decision(self):
        doc = openapi_document("https://inkcast.example.com")
        cancel = doc["paths"]["/api/v1/posts/{post_id}/cancel"]["post"]
        self.assertEqual(cancel["summary"], "Cancel a scheduled, recovering or unconfirmed post")
        resend = doc["paths"]["/api/v1/posts/{post_id}/resend"]["post"]
        self.assertEqual((resend["operationId"], resend["x-inkcast-scope"]), ("resendPost", "posts:write"))
        self.assertEqual(set(resend["responses"]) - {"200"}, {"401", "403", "404", "409"})
        self.assertEqual(route_label("/api/v1/posts/pst_0123456789ab/resend"), "/api/v1/posts/{post_id}/resend")
        from inkcast.libs.kpi import BY_NAME
        self.assertNotIn("Scheduled posts cancelled before they went out", BY_NAME["posts_cancelled"].description)
        self.assertIn("unconfirmed", BY_NAME["posts_cancelled"].description)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertRegex(readme, r"\| `inkcast_resend` \|")
        self.assertRegex(readme, r"\| `inkcast_cancel` \| Cancels a scheduled, recovering or unconfirmed post")
        git = (ROOT / "docs/git-publishing.md").read_text(encoding="utf-8")
        self.assertIn("inkcast_resend", git)
        self.assertRegex(git, r"\| `skipped` \|[^\n]*recovering")
        self.assertRegex(git, r"\| `skipped` \|[^\n]*unconfirmed")
        runbook = (ROOT / "docs/runbooks/alerts.md").read_text(encoding="utf-8")
        unconfirmed = runbook[runbook.index("## InkcastUnconfirmedPublishes"):runbook.index("## InkcastPostRecoveringTooLong")]
        self.assertIn("inkcast_resend", unconfirmed)
        self.assertNotIn("resolve themselves on the next sync", unconfirmed)
        self.assertIn("never creates", unconfirmed)

    def test_the_mcp_tool_titles(self):
        source = (ROOT / "src/inkcast/services/publishing/mcp_tools.py").read_text(encoding="utf-8")
        self.assertNotIn('title="Cancel a scheduled post"', source)
        self.assertIn('@mcp.tool(title="Cancel a pending post"', source)
        self.assertIn('@mcp.tool(title="Resend an unconfirmed post"', source)


if __name__ == "__main__":
    unittest.main()
