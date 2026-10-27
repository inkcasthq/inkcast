# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #221 (issue #216): a 'recovering' post can be cancelled, raises an alert after an hour,
and is capped, so it is never sent late without a person deciding.

Each ``Finding`` test failed on the PR's head before the fix committed with it; the ``Held`` tests pin what the fix
must keep. Concurrency is driven by hooks inside the platform's calls, never by sleeps."""

import asyncio
import logging
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.services.publishing.sync import GitSync

DAY = 86400


class Hooked(Platform):
    """A platform that can be checked (``find``). ``delivered`` is what reached it. ``refuse``: every lookup fails for a
    reason that will pass (a 429), so recovery defers. ``during_publish`` (an async hook) runs inside the next publish
    call, while the request is out at the platform; it unsets itself, and an exception it raises is the platform's
    answer (nothing was delivered then)."""

    id, name, default_api_url, credential_fields = "hooked", "Hooked", "https://hooked.invalid", []
    capabilities = frozenset({"find"})

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.delivered: list[str] = []
        self.lookups = 0
        self.refuse = True
        self.during_publish = None

    async def verify(self, creds):
        return Account(id="1", name="hooked")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        if self.during_publish:
            hook, self.during_publish = self.during_publish, None
            await hook(prepared)
        self.delivered.append(prepared.title)
        return PublishResult(url=f"https://hooked/{len(self.delivered)}", remote_id=str(len(self.delivered)),
                             status="published")

    async def find_existing(self, creds, account, prepared):
        self.lookups += 1
        if self.refuse:
            raise PlatformError("Hooked rate limit hit; try again in 15 minutes.", retryable=True, retry_after=900)
        if prepared.title in self.delivered:
            return PublishResult(url="https://hooked/found", remote_id="found", status="published")
        return None


class Harness(unittest.TestCase):
    """One database file, opened by the scheduler replica under test (``publisher``) and, where a test needs them, by
    other processes on their own connections (another replica, the API)."""

    def setUp(self):
        self.path = tempfile.mkdtemp() + "/inkcast.db"
        self.store = self.open()
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = Hooked()
        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "hooked"})
        self.publisher = self.replica()
        quiet = logging.getLogger("inkcast.libs.pipeline")
        level, quiet.level = quiet.level, logging.CRITICAL   # recovery's warnings are expected; keep the run readable
        self.addCleanup(setattr, quiet, "level", level)

    def open(self) -> Store:
        store = Store(self.path, "k" * 40)
        self.addCleanup(store.close)
        return store

    def replica(self) -> Publisher:
        """Another process on the same database, with its own connection."""
        return Publisher(self.open(), {self.platform.id: self.platform})

    def post(self, status: str, title: str = "t", run_at: float | None = None) -> str:
        prepared = self.platform.prepare({"title": title}, "Body.")
        return self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title=title,
                                      status=status, run_at=time.time() - 1 if run_at is None else run_at,
                                      payload=prepared.to_dict())

    def tick(self, later: float = 0.0, publisher: Publisher | None = None):
        return asyncio.run((publisher or self.publisher).run_due(now=time.time() + later))

    def status(self, post_id: str) -> str:
        return self.store.get_post(post_id)["status"]


# ---- 1. a cancel of a 'recovering' post is overwritten by the original send's late answer ---------------------------


class FindingTheSendersLateAnswerOverwritesTheCancel(Harness):
    """#216 makes 'recovering' cancellable, and makes recovery's writes yield to the cancel (``only_if``). But a post
    is 'recovering' because another replica's stuck pass took it while its send looked dead: the send may still be
    out (a reply trickled in under the per-read timeout, a paused process, a write held up behind the database), and
    ``renew_claim`` exists because a batch can be overtaken. Before #216 nothing could cancel a post in that state;
    now the person can, and the sender's answer, written unconditionally, lands on top of the cancel: a retryable
    failure puts the cancelled post back in the queue and the next tick sends it; a definite or uncertain failure
    turns it 'failed' or 'unconfirmed', which Git sync then creates again."""

    def cancelled_while_sending(self, answer: PlatformError | None) -> str:
        post_id = self.post("scheduled")
        other, api = self.replica(), self.replica()
        seen = []

        async def overtaken(prepared):
            # Worker A's request is out. Worker B's stuck pass takes the post over (its lookup is refused, so it stays
            # 'recovering'), and the person cancels it through the API. (Asserting in here would be swallowed: the
            # pipeline reads any exception from the plug-in as a crash.)
            await other.run_due(now=time.time() + STUCK_AFTER + 1)
            seen.append(self.status(post_id))
            seen.append(api.cancel(self.user, post_id))
            if answer is not None:
                raise answer

        self.platform.during_publish = overtaken
        self.tick()                                                           # worker A's tick: its send, then its answer
        self.assertEqual(seen, ["recovering", "t"])
        return post_id

    def test_a_retryable_failure_does_not_put_the_cancelled_post_back_in_the_queue(self):
        post_id = self.cancelled_while_sending(PlatformError("Hooked is unreachable.", retryable=True))
        self.assertEqual(self.status(post_id), "cancelled")
        self.tick(later=DAY)                                                  # long after any retry delay
        self.assertEqual((self.status(post_id), self.platform.delivered), ("cancelled", []), "sent after the cancel")

    def test_a_failure_or_an_uncertain_answer_does_not_overwrite_the_cancel(self):
        for answer in (PlatformError("Hooked refused the post (400)."),
                       PlatformError("Hooked timed out after the request was sent.", uncertain=True)):
            with self.subTest(answer=str(answer)):
                post_id = self.cancelled_while_sending(answer)
                self.assertEqual(self.status(post_id), "cancelled")

    def test_a_git_synced_post_cancelled_while_its_send_was_out_stays_cancelled_at_the_next_sync(self):
        """The sync is the sender here (a "now" post, from the API process), and it records the send's answer in its
        ledger: 'failed' there, the next push creates the cancelled post again and sends it."""
        git = GitSync(self.store, {self.platform.id: self.platform}, self.publisher)
        markdown = "---\ntitle: Hello\nplatforms: [hooked]\n---\n\nBody.\n"
        req = SyncRequest(source="github.com/acme/blog", files=[SyncFile(path="posts/p.md", markdown=markdown)])
        other, api = self.replica(), self.replica()
        seen = []

        async def overtaken(prepared):
            # Asserting in here would be swallowed: the pipeline reads any exception from the plug-in as a crash.
            (post_id,) = [r["id"] for r in self.store._all("SELECT id FROM posts")]
            await other.run_due(now=time.time() + STUCK_AFTER + 1)
            seen.append(self.status(post_id))
            seen.append(api.cancel(self.user, post_id))
            raise PlatformError("Hooked refused the post (400).")

        self.platform.during_publish = overtaken
        asyncio.run(git.sync(self.user, req))
        self.assertEqual(seen, ["recovering", "Hello"])
        (again,) = asyncio.run(git.sync(self.user, req)).results            # the next push, the file unchanged
        self.assertEqual((again.action, again.status, self.platform.delivered), ("unchanged", "cancelled", []),
                         "the next sync sent the cancelled post")


class HeldTheSendersSuccessIsStillRecorded(Harness):
    def test_a_post_the_send_delivered_after_the_cancel_is_recorded_published(self):
        """The truth beats the cancel: the post is out, and the person needs its URL to remove it."""
        post_id = self.post("scheduled")
        other, api = self.replica(), self.replica()

        async def overtaken(prepared):
            await other.run_due(now=time.time() + STUCK_AFTER + 1)
            api.cancel(self.user, post_id)

        self.platform.during_publish = overtaken
        self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], self.platform.delivered),
                         ("published", "https://hooked/1", ["t"]))


# ---- 1b. the same late answer, written over a post another worker has already re-sent ----------------------------


class FindingTheSendersLateFailureReSendsAPostAnotherWorkerSent(Harness):
    """The other half of the takeover that recovery's ``only_if`` (#211) guards: worker B takes the post over while A's
    request is out, the platform says it is missing (A's request never arrived), and B's pass re-queues and sends it:
    'published'. A's answer then lands, unconditionally: a retryable failure puts the published post back in the
    queue, and the next tick posts it a second time; a definite one marks it 'failed', which Git sync creates again.
    A retryable or definite failure says the request never took effect, so it is written only while the post is
    still A's ('publishing'). Not new in #221, but the PR's invariant ("never sent twice") rests on it."""

    def re_sent_while_sending(self, answer: PlatformError) -> str:
        post_id = self.post("scheduled")
        other = self.replica()
        seen = []

        async def overtaken(prepared):
            self.platform.refuse = False                                      # B asks: not there
            await other.run_due(now=time.time() + STUCK_AFTER + 1)            # B: re-queued, then sent by the same pass
            seen.append((self.status(post_id), list(self.platform.delivered)))
            raise answer

        self.platform.during_publish = overtaken
        self.tick()                                                           # worker A's tick: its send, then its answer
        self.assertEqual(seen, [("published", ["t"])])
        return post_id

    def test_a_retryable_failure_does_not_send_the_post_again(self):
        post_id = self.re_sent_while_sending(PlatformError("Hooked is unreachable.", retryable=True))
        self.tick(later=DAY)                                                  # long after any retry delay
        self.assertEqual((self.status(post_id), self.platform.delivered), ("published", ["t"]), "posted twice")

    def test_a_definite_failure_does_not_mark_the_published_post_failed(self):
        post_id = self.re_sent_while_sending(PlatformError("Hooked refused the post (400)."))
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"]), ("published", "https://hooked/1"))


class HeldAnUncertainLateAnswerStillGoesToAPerson(Harness):
    def test_a_re_sent_post_whose_first_request_may_have_arrived_is_unconfirmed_with_its_url(self):
        """A's request may have reached the platform after B's lookup: the post may be there twice. A person checks;
        B's URL stays on the row."""
        post_id = self.post("scheduled")
        other = self.replica()

        async def overtaken(prepared):
            self.platform.refuse = False
            await other.run_due(now=time.time() + STUCK_AFTER + 1)
            raise PlatformError("Hooked timed out after the request was sent.", uncertain=True)

        self.platform.during_publish = overtaken
        self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"]), ("unconfirmed", "https://hooked/1"))


# ---- 2. the cap's clock starts at the stuck pass, not at the interruption ---------------------------------------


class FindingTheCapStartsAtTheStuckPassNotTheInterruption(Harness):
    """``claim_stuck`` sets ``recovering_since`` to the pass's own clock for a post it takes from 'publishing'. A post
    interrupted mid-send while no scheduler ran for a while (an outage, a crash loop, the worker scaled down) starts
    its 24 hours only when a scheduler comes back: the platform's first answer, "missing", re-queues it and it goes
    out days after its send was interrupted, with nobody deciding. The cap is meant to bound exactly that ("the
    ceiling bounds how late a post the platform finally reports missing may go out on its own"). The gauge, read
    from the same column, says the post has been recovering for 0 s. A post already 'recovering' when the column was
    added (since #199, the posts #216 is about) has no ``recovering_since`` either, and gets the same fresh 24 hours
    from the first pass after the upgrade."""

    def interrupted(self, ago: float) -> str:
        """A post the process was sending ``ago`` seconds ago when it stopped; no stuck pass has run since."""
        post_id = self.post("publishing")
        self.store._exec("UPDATE posts SET updated_at = ? WHERE id = ?", time.time() - ago, post_id)
        return post_id

    def test_a_post_interrupted_two_days_before_the_scheduler_came_back_is_never_sent_on_its_own(self):
        self.platform.refuse = False                                          # the platform answers: not there
        post_id = self.interrupted(2 * DAY)
        self.tick()                                                           # the scheduler is back: its stuck pass
        self.tick(later=1)                                                    # and its next tick
        self.assertEqual((self.status(post_id), self.platform.delivered), ("unconfirmed", []),
                         "sent two days after its send was interrupted")

    def test_the_gauge_counts_from_when_the_post_was_stuck(self):
        post_id = self.interrupted(3 * 3600)                                  # three hours before the stuck pass
        self.tick()                                                           # the lookup is refused: 'recovering'
        self.assertEqual(self.status(post_id), "recovering")
        self.assertGreater(self.store.oldest_recovering_age(time.time()), 3600, "InkcastPostRecoveringTooLong stays quiet")

    def test_a_post_recovering_since_before_the_upgrade_does_not_start_its_hours_afresh(self):
        self.platform.refuse = False                                          # the platform answers at last: not there
        post_id = self.post("recovering", run_at=time.time() - 2 * DAY)       # due, sent and interrupted two days ago
        self.store._exec("UPDATE posts SET updated_at = ?, recovering_since = NULL WHERE id = ?",
                         time.time() - STUCK_AFTER - 1, post_id)              # its last pass, before the column existed
        self.tick()                                                           # the first stuck pass after the upgrade
        self.tick(later=1)
        self.assertEqual((self.status(post_id), self.platform.delivered), ("unconfirmed", []),
                         "sent two days after its send was interrupted")


class HeldAFreshInterruptionIsStillRecovered(Harness):
    def test_a_post_interrupted_minutes_ago_that_the_platform_lacks_is_sent_again(self):
        self.platform.refuse = False
        post_id = self.post("publishing")
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        self.tick()
        self.assertEqual(self.status(post_id), "scheduled")
        self.tick(later=1)
        self.assertEqual((self.status(post_id), self.platform.delivered), ("published", ["t"]))


if __name__ == "__main__":
    unittest.main()
