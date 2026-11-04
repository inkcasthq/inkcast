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
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.libs.platforms.medium import MediumPlatform


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


# ---- item 2: a 2xx that names nothing is not a confirmed answer --------------------------------------------------


def answering(status: int, body: bytes):
    """Both built-in plug-ins against an API that answers every request with this status and body, and the requests."""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(status, content=body, headers={"Content-Type": "application/json"} if body else {})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return [DevtoPlatform(http), MediumPlatform(http)], requests


EMPTY_ANSWERS = (
    (204, b""), (202, b""), (200, b""), (201, b""),       # no body at all
    (201, b"{}"), (201, b'{"data": {}}'), (201, b'{"ok": true}'),   # a body without the post
)


class EmptySuccessOnAPublish(unittest.TestCase):
    """Neither platform documents such an answer to a create: Forem's ``POST /articles`` is ``201`` with the article
    (app/controllers/concerns/api/articles_controller.rb, ``render "show", status: :created, location: @article.url``),
    Medium's ``POST /users/{id}/posts`` is ``201`` with the post in a ``data`` envelope (medium-api-docs §3.3). So a 2xx
    naming nothing is an answer we can't read: the request was accepted, the post is probably out, where is unknown."""

    def publisher(self, platform: Platform) -> Publisher:
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.store.save_credentials(self.user, platform.id, {"api_key": "k", "token": "t"},
                                    {"id": "1", "name": "on-call"})
        return Publisher(self.store, {platform.id: platform})

    def test_a_now_publish_is_unconfirmed_never_published_with_nothing_to_find_it_by(self):
        for status, body in EMPTY_ANSWERS:
            platforms, requests = answering(status, body)
            for platform in platforms:
                with self.subTest(platform=platform.id, status=status, body=body):
                    publisher = self.publisher(platform)
                    story = parse_story("# Now\n\nBody.\n", known_platforms=[platform.id])
                    sent = len(requests)
                    (outcome,) = asyncio.run(publisher.publish(self.user, story, [platform.id]))
                    post = self.store.get_post(outcome.post_id)
                    self.assertEqual((outcome.status, post["status"]), ("unconfirmed", "unconfirmed"))
                    self.assertEqual((outcome.url, post["result_url"], post["remote_id"]), (None, None, None))
                    self.assertIn(platform.name, post["error"])
                    self.assertIn("may or may not have been published", post["error"])
                    self.assertNotIn("internal error", post["error"], "an unreadable answer is not a plug-in bug")
                    self.assertEqual(len(requests) - sent, 1)

    def test_a_scheduled_post_is_unconfirmed_once_and_never_sent_again(self):
        for status, body in ((204, b""), (201, b"{}")):
            platforms, requests = answering(status, body)
            for platform in platforms:
                with self.subTest(platform=platform.id, status=status):
                    publisher = self.publisher(platform)
                    prepared = platform.prepare({"title": "t"}, "Body.")
                    post_id = self.store.create_post(group_id="g", user_id=self.user, platform=platform.id, title="t",
                                                     status="scheduled", run_at=time.time() - 1,
                                                     payload=prepared.to_dict())
                    sent = len(requests)
                    asyncio.run(publisher.run_due())
                    post = self.store.get_post(post_id)
                    self.assertEqual((post["status"], post["attempts"], len(requests) - sent), ("unconfirmed", 1, 1))
                    for later in (61, 301, 1801, 7 * 24 * 3600):
                        asyncio.run(publisher.run_due(now=time.time() + later))
                    self.assertEqual((self.store.get_post(post_id)["status"], len(requests) - sent), ("unconfirmed", 1))

    def test_a_draft_is_held_to_the_same_rule(self):
        """DEV's ``published: false`` reads 'draft' from the payload when the answer says nothing: no URL, no id,
        and a 'draft' status that the pipeline would have recorded as a confirmed draft."""
        platforms, _ = answering(204, b"")
        devto = platforms[0]
        publisher = self.publisher(devto)
        story = parse_story("---\ntitle: Draft\ndevto:\n  draft: true\n---\n\nBody.\n", known_platforms=[devto.id])
        (outcome,) = asyncio.run(publisher.publish(self.user, story, [devto.id]))
        self.assertEqual((outcome.status, self.store.get_post(outcome.post_id)["status"]), ("unconfirmed", "unconfirmed"))

    def test_an_answer_naming_the_id_or_the_url_is_a_confirmed_publish(self):
        """Either is enough to find the post again (``update``, ``find_existing``, the person): the rule is "neither"."""
        cases = (
            ("devto", 201, b'{"id": 7, "published": true}', (None, "7")),
            ("devto", 201, b'{"url": "https://dev.to/on-call/x", "published": true}', ("https://dev.to/on-call/x", None)),
            ("medium", 201, b'{"data": {"id": "e6f36a", "publishStatus": "public"}}', (None, "e6f36a")),
        )
        for pid, status, body, (url, remote_id) in cases:
            platforms, _ = answering(status, body)
            platform = next(p for p in platforms if p.id == pid)
            with self.subTest(platform=pid, body=body):
                publisher = self.publisher(platform)
                story = parse_story("# Now\n\nBody.\n", known_platforms=[platform.id])
                (outcome,) = asyncio.run(publisher.publish(self.user, story, [platform.id]))
                post = self.store.get_post(outcome.post_id)
                self.assertEqual((outcome.status, post["status"], post["result_url"], post["remote_id"]),
                                 ("published", "published", url, remote_id))


class EmptySuccessFromAnyPlugin(Harness):
    def test_a_plugin_returning_a_result_that_names_nothing_is_unconfirmed_too(self):
        """A third-party plug-in that returns ``PublishResult(None, None, "published")`` is held to the same rule: the
        pipeline, not the plug-in, is where a post is marked 'published'."""
        post_id = self.post()
        self.platform.answer = PublishResult(url=None, remote_id=None, status="published")
        (outcome,) = asyncio.run(self.publisher.run_due(now=time.time()))
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], post["result_url"], post["remote_id"]),
                         ("unconfirmed", "unconfirmed", None, None))
        self.assertIn("Findable accepted the request", post["error"])
        self.assertEqual(self.published_events(post_id), 0)
        self.assertEqual(self.platform.sent, ["t"])
        self.assertEqual(asyncio.run(self.publisher.run_due(now=time.time() + 7 * 24 * 3600)), [], "sent again")


# ---- item 3: 301/302 on a create stay definite failures, by design -----------------------------------------------


class RedirectsOnACreate(unittest.TestCase):
    """Kept as #173 decided (PR #192): a 301 or 302 to a create is a definite, non-retryable failure; only a 303 is
    uncertain. RFC 9110 §15.4.2 (301) and §15.4.3 (302) say the target resource "has been assigned a new permanent
    URI" / "resides temporarily under a different URI" and the client "ought to" re-request there (a user agent "MAY
    change the request method from POST to GET"): the server has not acted on the request. Only §15.4.4 (303) is
    "an indirect response to the original request", "the output of a POST action": acted on, result elsewhere. Neither
    supported platform answers a create with a 3xx after acting: Forem renders ``201`` with the article
    (app/controllers/concerns/api/articles_controller.rb ``create``), Medium documents ``201`` with a ``data``
    envelope and errors 400/401/403 (medium-api-docs §3.3). A 301/302 from these APIs is a wrong API URL (http to
    https, a moved Forem host), for which "failed: check INKCAST_API_URL_<ID>" is the right answer; 'unconfirmed'
    would park every Git-synced file behind a person's decision instead."""

    def test_301_and_302_on_a_create_stay_definite_and_303_stays_uncertain(self):
        for status, flags in ((301, (False, False)), (302, (False, False)), (303, (False, True))):
            requests: list[httpx2.Request] = []

            def handler(request: httpx2.Request, status=status) -> httpx2.Response:
                requests.append(request)
                return httpx2.Response(status, headers={"Location": "https://elsewhere.invalid/api/articles/1"})

            http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
            for platform in (DevtoPlatform(http), MediumPlatform(http)):
                with self.subTest(platform=platform.id, status=status):
                    prepared = platform.prepare({"title": "t"}, "Body.")
                    with self.assertRaises(PlatformError) as caught:
                        asyncio.run(platform.publish({"api_key": "k", "token": "t"}, Account(id="1", name="n"),
                                                     prepared, None))
                    self.assertEqual((caught.exception.retryable, caught.exception.uncertain), flags)
                    self.assertIn(str(status), str(caught.exception))
            self.assertEqual(len(requests), 2, "the redirect was never followed, so the POST was never replayed")


if __name__ == "__main__":
    unittest.main()
