# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #231 (issue #226): a person decides on an 'unconfirmed' post.

Each ``Finding`` class's first test failed on the PR's head and passes with the fix committed with it; the tests after
it pin what still holds, on both. Concurrency is driven by hooks inside the platform's calls and the store's reads,
never by sleeps; a second process is a second ``Store`` on the same file, run to its end at the hook (a thread joined
there, so its event loop is its own)."""

import asyncio
import threading
import unittest

from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import PlatformError
from inkcast.services.publishing.usecases import InkcastService

from test_person_decides import SyncHarness


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


if __name__ == "__main__":
    unittest.main()
