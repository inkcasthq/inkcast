# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #231 (issue #226): a person decides on an 'unconfirmed' post.

Each ``Finding`` class's first test failed on the PR's head and passes with the fix committed with it; the tests after
it pin what still holds, on both. Concurrency is driven by hooks inside the platform's calls and the store's reads,
never by sleeps; a second process is a second ``Store`` on the same file, run to its end at the hook (a thread joined
there, so its event loop is its own)."""

import asyncio
import threading
import time
import unittest

from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import PlatformError
from inkcast.services.publishing.usecases import InkcastService, ServiceError

from test_person_decides import Harness, SyncHarness


def in_another_process(work):
    """Run ``work`` (a coroutine function) to its end on its own event loop, as another process would, and return its
    result: the caller's loop is blocked in a synchronous hook meanwhile, holding no transaction."""
    out: list = []
    thread = threading.Thread(target=lambda: out.append(asyncio.run(work())))
    thread.start()
    thread.join()
    return out[0] if out else None


class FindingASyncThatReadTheEntryBeforeAResendDoesNotUndoIt(SyncHarness):
    def test_the_entry_keeps_the_new_post_and_an_edit_is_not_a_second_copy(self):
        """A push (the API, one process) reads the file's entry, still on the unconfirmed post; the person's resend (the
        API, another process) then runs: the old post is cancelled, the entry is re-pointed at the new post, which is
        published. The push then reads the old post, 'cancelled', and "follows" it, with ``put_entry``: that keeps the
        entry's current post (the new one) but writes the old one's status, so the entry reads 'cancelled'. The edited
        file in the push is then a brand-new post, sent: a second copy nobody decided on (and a later edit of an
        unchanged file would be one too, instead of an update or a stale note)."""
        self.platform.answer = PlatformError("Findable timed out after the request was sent.", uncertain=True)
        first = self.sync()
        old_id = first.post_id
        self.assertEqual((first.action, first.status, self.entry()["status"]), ("create", "unconfirmed", "unconfirmed"))
        api_store = self.open()
        api_publisher = Publisher(api_store, self.platforms)
        api = InkcastService(api_store, self.platforms, api_publisher, "https://inkcast.example.com")
        real_get_entry = self.store.get_entry
        resent = []

        def get_entry(*args):
            entry = real_get_entry(*args)
            if not resent:
                resent.append(in_another_process(lambda: api.resend(self.user, old_id)))
            return entry

        self.store.get_entry = get_entry
        self.sync("Better body.")
        (info,) = resent
        self.assertEqual(info.status, "published")
        self.assertEqual(self.platform.sent, ["Hello"], "the edit was sent as a second copy")
        entry = self.entry()
        self.assertEqual((entry["post_id"], entry["status"], entry["url"]), (info.id, "published", info.url))
        again = self.sync("Better body.")                                    # the next push: the new post, not a third
        self.assertEqual((again.action, again.post_id, self.platform.sent), ("stale", info.id, ["Hello"]))


class FindingTheSendersLateUncertainAnswerDoesNotUndoAConfirmedPost(Harness):
    def test_a_post_recovery_found_on_the_platform_stays_published(self):
        """Worker A's request reached the platform and its answer is slow; worker B's stuck pass takes the post over and
        Findable says it has it: 'published'. A's answer then lands, a timeout after the request was sent (uncertain),
        and was written over the platform's own confirmation (``unless="cancelled"`` only): the post read
        'unconfirmed', with the alert and the "check, then resend it" note, and the resend this PR adds then sent it a
        second time. A plug-in crash reading the reply is the same uncertain answer."""
        answers = {"uncertain": PlatformError("Findable timed out after the request was sent.", uncertain=True),
                   "crash": RuntimeError("the plug-in fell over reading the reply")}
        for how, answer in answers.items():
            with self.subTest(how=how):
                post_id = self.post("scheduled", how)
                b = self.replica()
                seen = []

                async def while_a_waits(prepared, post_id=post_id, b=b, seen=seen, answer=answer):
                    self.platform.sent.append(prepared.title)                 # A's request reached Findable
                    await b.run_due(now=time.time() + STUCK_AFTER + 1)        # B: stuck → recovering → found → published
                    seen.append(self.status(post_id))
                    raise answer

                self.platform.during_publish = while_a_waits
                self.tick()
                self.assertEqual(seen, ["published"])
                post = self.store.get_post(post_id)
                self.assertEqual((post["status"], post["result_url"]), ("published", "https://findable/found"),
                                 "A's uncertain answer was written over the platform's confirmation")
                with self.assertRaises(ServiceError) as caught:              # nothing for the person to decide
                    asyncio.run(self.service.resend(self.user, post_id))
                self.assertEqual(caught.exception.code, "not_resendable")
                self.assertEqual(self.platform.sent.count(how), 1, "sent twice")

    def test_an_uncertain_answer_with_nobody_else_holding_the_post_is_still_written(self):
        post_id = self.post("scheduled")
        self.platform.answer = PlatformError("Findable timed out after the request was sent.", uncertain=True)
        self.tick()
        self.assertEqual(self.status(post_id), "unconfirmed")


if __name__ == "__main__":
    unittest.main()
