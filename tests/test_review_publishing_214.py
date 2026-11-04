# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #214 (from the adversarial reviews of PRs #189 and #192): the writes after a send, and an empty 2xx.

1. The writes after a send must be the send's own: a worker whose claim expired (its send outlasted STUCK_AFTER and
   another worker took the post over and re-sent it) must not overwrite that worker's outcome. The failure writes
   were fenced by #226 (``only_if="publishing", if_attempts=held``; tests/test_person_decides.py), and the uncertain
   write yields to a cancel and to a lookup that found this very send. The success write had no fence at all: a late
   2xx wrote 'published' over another worker's re-send while its request was still out, and over that re-send's own
   record. Fixed here with the attempt count alone (``if_attempts=held``), not the status: a success is the truth
   about its request, and #216 decided it is written over a cancel (tests/test_review_recovering_posts.py), so only a
   re-send by another worker makes the row not this send's.
2. A 2xx that names neither the post's URL nor its id (a 204, a 202 with no body, a JSON body without the post) marked
   the post 'published' with nothing to find it by. The platform accepted the request (RFC 9110 §15.3), so the post is
   probably out, but where is unknown: that is an uncertain answer, 'unconfirmed' for a person like a lost one. Fixed
   here at the one place a post is marked 'published', so every plug-in is covered.
3. 301/302 on a create stay definite failures, by design: see ``RedirectsOnACreate``.

The ``LateSuccess…`` tests and the ``EmptySuccess…`` tests (but the id-or-URL-alone one) failed on main before the fix
committed with them; the ``Held…`` tests, that one and item 3's pin what holds."""

import asyncio
import logging
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.content.story import parse_story
from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult


class Findable(Platform):
    """A platform that can be checked (``find``): it finds what reached it (``sent``), or refuses every lookup with a
    429 while ``refuse`` is set. ``during_publish(prepared)`` (an async hook) runs inside the next publish while the
    request is out, before it reaches the platform; it unsets itself first. ``answer`` is what the next publish
    returns instead of the usual result."""

    id, name, default_api_url, credential_fields = "findable", "Findable", "https://findable.invalid", []
    capabilities = frozenset({"find"})

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sent: list[str] = []
        self.refuse = False
        self.during_publish = None
        self.answer: PublishResult | None = None

    async def verify(self, creds):
        return Account(id="1", name="findable")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        if self.during_publish:
            hook, self.during_publish = self.during_publish, None
            await hook(prepared)
        self.sent.append(prepared.title)
        if self.answer is not None:
            answer, self.answer = self.answer, None
            return answer
        return PublishResult(url=f"https://findable/{len(self.sent)}", remote_id=str(len(self.sent)), status="published")

    async def find_existing(self, creds, account, prepared):
        if self.refuse:
            raise PlatformError("Findable rate limit hit; try again in 15 minutes.", retryable=True, retry_after=900)
        return (PublishResult(url="https://findable/found", remote_id="found", status="published")
                if prepared.title in self.sent else None)


class Harness(unittest.TestCase):
    """One database file, opened by the scheduler under test (``publisher``) and by other replicas on their own
    connections."""

    def setUp(self):
        self.path = tempfile.mkdtemp() + "/inkcast.db"
        self.store = self.open()
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = Findable()
        self.platforms = {self.platform.id: self.platform}
        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
        self.publisher = Publisher(self.store, self.platforms)
        quiet = logging.getLogger("inkcast.libs.pipeline")
        level, quiet.level = quiet.level, logging.CRITICAL   # recovery's warnings are expected; keep the run readable
        self.addCleanup(setattr, quiet, "level", level)

    def open(self) -> Store:
        store = Store(self.path, "k" * 40)
        self.addCleanup(store.close)
        return store

    def replica(self) -> Publisher:
        return Publisher(self.open(), self.platforms)

    def post(self, status: str = "scheduled", title: str = "t") -> str:
        prepared = self.platform.prepare({"title": title}, "Body.")
        return self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title=title,
                                      status=status, run_at=time.time() - 1, payload=prepared.to_dict())

    def status(self, post_id: str) -> str:
        return self.store.get_post(post_id)["status"]

    def published_events(self, post_id: str) -> int:
        return sum(1 for e in self.store.poll("audit", limit=200) if e.subject == post_id and e.type == "post.published")


# ---- item 1: the success write is the send's own, at its attempt count, like the failure writes ----------------


class LateSuccessIsNotWrittenOverAnotherWorkersSend(Harness):
    def test_a_success_landing_after_another_worker_re_sent_the_post_keeps_that_workers_record(self):
        """Worker A's send is out past STUCK_AFTER. Worker B's stuck pass takes the post over, is told "missing" (A's
        request has not arrived yet), re-queues it and sends it: 'published' with B's URL, at one attempt more. A's
        2xx then lands. Written unconditionally, it replaced B's URL and id with A's and wrote a second post.published
        event: the row named A's copy, the ledger and the person B's. A's answer is written only at A's own attempt
        count, logged otherwise, and A is told what the post is now."""
        post_id = self.post()
        b = self.replica()
        seen = []

        async def while_a_waits(prepared):                                    # inside A's publish
            await b.run_due(now=time.time() + STUCK_AFTER + 1)                # B: missing → re-queued → sent
            seen.append((self.status(post_id), self.store.get_post(post_id)["result_url"]))

        self.platform.during_publish = while_a_waits
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING") as logged:
            (outcome,) = asyncio.run(self.publisher.run_due(now=time.time()))  # A's tick: its success lands last
        self.assertEqual(seen, [("published", "https://findable/1")])
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], post["remote_id"], post["attempts"]),
                         ("published", "https://findable/1", "1", 2), "A's late success replaced B's record")
        self.assertEqual((outcome.status, outcome.url), ("published", "https://findable/1"), "what the post is now")
        self.assertEqual(self.published_events(post_id), 1, "one record, one event")
        self.assertEqual(self.platform.sent, ["t", "t"], "two copies went out: the lookup raced A's request")
        late = [r for r in logged.records if getattr(r, "event", None) == "publish.late_answer"]
        self.assertEqual(len(late), 1, "the copy the post no longer names is in the log")
        self.assertIn("https://findable/2", late[0].getMessage())

    def test_a_success_landing_while_another_worker_re_sends_is_not_written_over_that_send(self):
        """Worker A's send is out. Worker B's stuck pass takes the post, is told "missing" (A's request has not
        arrived yet), re-queues it and starts sending it; B's request is out when A's 2xx lands. Written over B's
        'publishing', the post read 'published' with A's URL while B's send was still out, and B's own answer then
        wrote a second post.published event over it. A's answer is written only at A's own attempt count."""
        post_id = self.post()
        b = self.replica()
        b_out, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def b_sends(prepared):                                          # inside B's publish: hold its request
            b_out.set()
            await release.wait()

        async def while_a_waits(prepared):                                    # inside A's publish
            self.platform.during_publish = b_sends
            self.tasks.append(asyncio.create_task(b.run_due(now=time.time() + STUCK_AFTER + 1)))   # B: missing → re-queued → sending
            await b_out.wait()
            seen.append(("b out", self.status(post_id), self.store.get_post(post_id)["attempts"]))

        async def run():
            self.tasks = []
            (outcome,) = await self.publisher.run_due(now=time.time())        # A's tick
            seen.append(("a done", self.status(post_id), outcome.status))
            release.set()
            await asyncio.gather(*self.tasks)

        self.platform.during_publish = while_a_waits
        asyncio.run(run())
        self.assertEqual(seen[0], ("b out", "publishing", 2))
        self.assertEqual(seen[1], ("a done", "publishing", "publishing"), "A's late success was written over B's send")
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], post["attempts"]), ("published", "https://findable/2", 2))
        self.assertEqual(self.published_events(post_id), 1, "one send's answer, one event")


class HeldTheSendersOwnSuccessIsStillWritten(Harness):
    def test_a_success_at_the_sends_own_attempt_count_is_the_truth_over_a_takeover_or_a_cancel(self):
        """The fence is the attempt count alone, not the status: a 2xx says this request published the post, so it is
        written over what the post became while the send was out, as long as nobody re-sent it. Over a takeover
        whose lookup the platform refused ('recovering'): published, and the next stuck pass leaves it. Over the
        person's cancel of that recovering post: published, as #216 decided (tests/test_review_recovering_posts.py:
        the post is out, and the person needs its URL to remove it)."""
        for cancel in (False, True):
            with self.subTest(cancelled_meanwhile=cancel):
                post_id = self.post()
                b = self.replica()

                async def while_a_waits(prepared):
                    self.platform.refuse = True
                    await b.run_due(now=time.time() + STUCK_AFTER + 1)        # B: recovering, lookup refused
                    self.platform.refuse = False
                    if cancel:
                        self.publisher.cancel(self.user, post_id)

                self.platform.during_publish = while_a_waits
                (outcome,) = asyncio.run(self.publisher.run_due(now=time.time()))
                post = self.store.get_post(post_id)
                self.assertEqual((outcome.status, post["status"], post["attempts"]), ("published", "published", 1))
                self.assertEqual(post["result_url"], outcome.url)
                asyncio.run(b.run_due(now=time.time() + 2 * (STUCK_AFTER + 1)))   # the next stuck pass: nothing to do
                self.assertEqual(self.status(post_id), "published")

    def test_a_success_with_nobody_else_holding_the_post_is_recorded_from_every_entry_point(self):
        """The scheduler's claim holds the post at its attempt count; a "now" post's row and a resend's new row still
        read 0 attempts. Each send's own success is written as before."""
        scheduled = self.post(title="s")
        asyncio.run(self.publisher.run_due(now=time.time()))
        post = self.store.get_post(scheduled)
        self.assertEqual((post["status"], post["attempts"], post["result_url"]), ("published", 1, "https://findable/1"))

        story = parse_story("# Now\n\nBody.\n", known_platforms=[self.platform.id])
        (now_outcome,) = asyncio.run(self.publisher.publish(self.user, story, [self.platform.id]))
        post = self.store.get_post(now_outcome.post_id)
        self.assertEqual((now_outcome.status, post["status"], post["attempts"], post["result_url"]),
                         ("published", "published", 0, "https://findable/2"))

        unconfirmed = self.post(status="unconfirmed", title="u")
        resent = asyncio.run(self.publisher.resend(self.user, unconfirmed))
        post = self.store.get_post(resent.post_id)
        self.assertEqual((resent.status, post["status"], post["attempts"], post["result_url"]),
                         ("published", "published", 0, "https://findable/3"))
        self.assertEqual(self.platform.sent, ["s", "Now", "u"])


if __name__ == "__main__":
    unittest.main()
