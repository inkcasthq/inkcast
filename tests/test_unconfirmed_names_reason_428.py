# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #428: an unconfirmed post's error names the platform's reason it can't tell which post is this one.

When recovery, or Git sync's check, asks a platform whether a send arrived and the platform answers with a result that
names neither URL nor id (DEV since #396: more than one article agrees with the send, or one shares its title but not
the rest; the reason in its ``notes``), ``Publisher.confirm`` returned None, and the post was unconfirmed with "can't
be checked automatically": the reason reached only the log line. The person deciding was told nothing could be checked
when the platform was checked, and not which articles to look at. Now ``confirm`` returns an ``unconfirmed`` outcome
whose message is the platform's reason, worded for the person with the ids it names, and the post's error and the
sync's result carry it; None still means the platform could not be checked at all (no lookup, not connected, the lookup
refused, the plug-in crashed), with today's wording. Every ``Finding`` failed on main; the ``Held`` pins what holds."""

import asyncio
import logging
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, PublishResult
from inkcast.libs.platforms.devto import DevtoPlatform

from test_devto_lookup_396 import article
from test_person_decides import DAY, Blind, Findable, Harness, SyncHarness

NOT_FOR_THE_PERSON = "NEVER-IN-THE-ERROR"   # content of DEV's answer that must stay out of the post's error


def two_agreeing() -> list[dict]:
    """DEV's listing: two articles that agree with the send on everything DEV lets Inkcast compare."""
    return [{**article(1002, "Hello", "Body."), "description": NOT_FOR_THE_PERSON},
            {**article(1001, "Hello", "Body."), "description": NOT_FOR_THE_PERSON}]


class DevRecovery(unittest.TestCase):
    """A send to DEV interrupted mid-send, recovered against a mock DEV whose listing is ``self.listing`` and that
    answers 429 while ``self.refusing``."""

    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.listing: list[dict] = []
        self.refusing = False
        self.lookups = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.lookups += 1
            if self.refusing:
                return httpx2.Response(429, headers={"Retry-After": "900"}, json={"error": "rate limit"})
            return httpx2.Response(200, json=self.listing)

        self.devto = DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        self.store.save_credentials(self.user, self.devto.id, {"api_key": "k"}, {"id": "42", "name": "On Call"})
        self.publisher = Publisher(self.store, {self.devto.id: self.devto})
        quiet = logging.getLogger("inkcast.libs.pipeline")
        level = quiet.level
        quiet.setLevel(logging.CRITICAL)
        self.addCleanup(quiet.setLevel, level)

    def interrupted(self) -> str:
        prepared = self.devto.prepare({"title": "Hello"}, "Body.")
        post_id = self.store.create_post(group_id="g", user_id=self.user, platform=self.devto.id, title="Hello",
                                         status="publishing", run_at=time.time() - STUCK_AFTER - 2, payload=prepared.to_dict())
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        return post_id

    def tick(self, later: float = 0.0):
        return asyncio.run(self.publisher.run_due(now=time.time() + later))

    def assert_names_the_reason(self, error: str, outcome_message: str | None) -> None:
        self.assertIn("DEV can't say which article is this post: 2 articles agree with it: id 1002, id 1001", error,
                      "the platform's reason did not reach the post")
        self.assertNotIn("can't be checked automatically", error, "the person is told nothing was checked")
        self.assertNotIn(NOT_FOR_THE_PERSON, error, "content of the platform's answer reached the error")
        self.assertIn("resend it (inkcast_resend, or POST /api/v1/posts/", error)
        self.assertIn("cancel it (inkcast_cancel, or POST /api/v1/posts/", error)
        self.assertEqual(outcome_message, error)


class FindingAnUnconfirmedPostNamesThePlatformsReason(DevRecovery):
    def test_a_lookup_that_answers_two_matching_articles_leaves_the_post_unconfirmed_naming_both(self):
        post_id = self.interrupted()
        self.listing = two_agreeing()
        (outcome,) = self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], post["result_url"], post["remote_id"]),
                         ("unconfirmed", "unconfirmed", None, None), "one of the articles was recorded as the post")
        self.assert_names_the_reason(post["error"], outcome.message)
        self.assertEqual(self.lookups, 1)
        self.tick(later=STUCK_AFTER + 1)                                      # unconfirmed is the person's: not asked again
        self.assertEqual((self.store.get_post(post_id)["status"], self.lookups), ("unconfirmed", 1))


class FindingTheCapNamesTheReasonToo(DevRecovery):
    def test_the_caps_last_lookup_that_cannot_tell_names_the_articles_and_the_hours(self):
        post_id = self.interrupted()
        self.refusing = True
        self.tick()
        self.assertEqual(self.store.get_post(post_id)["status"], "recovering")
        self.refusing, self.listing = False, two_agreeing()
        (outcome,) = self.tick(later=DAY + 1)
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], post["remote_id"]), ("unconfirmed", "unconfirmed", None))
        self.assert_names_the_reason(post["error"], outcome.message)
        self.assertIn("24 hours since the send was interrupted (INKCAST_PUBLISH_RECOVERY_MAX_HOURS)", post["error"])
        self.assertNotIn("could not be asked", post["error"], "the person is told DEV was never asked")


class FindingOnlyIdsAndFixedTextReachTheError(DevRecovery):
    def test_an_article_whose_id_is_not_a_number_is_named_without_it(self):
        """DEV's ids are integers. An answer with anything else in an id names no id for that article, so nothing of
        the answer but ids and fixed text ever reaches the error."""
        post_id = self.interrupted()
        self.listing = [{**article(1001, "Hello", "Body.")}, {**article(1002, "Hello", "Body."), "id": "<b>not an id</b>"}]
        self.tick()
        error = self.store.get_post(post_id)["error"]
        self.assertIn("2 articles agree with it: id 1001, an article without an id", error)
        self.assertNotIn("<b>", error, "content of the platform's answer reached the error")
        shares = [{**article(1003, "Hello", "Other.", published=False), "id": True}]   # a bool is not an id either
        prepared = self.devto.prepare({"title": "Hello"}, "Body.")
        self.listing = shares
        found = asyncio.run(self.devto.find_existing({"api_key": "k"}, Account(id="42", name="On Call"), prepared))
        self.assertEqual((found.url, found.remote_id), (None, None))
        self.assertIn("an article without an id (other content, another status)", found.notes[0])


class HeldAPlatformWithoutALookupKeepsTodaysWording(Harness):
    platform_class = Blind

    def test_a_blind_platform_cannot_be_checked_automatically(self):
        post_id = self.post("publishing")
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        (outcome,) = self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"]), ("unconfirmed", "unconfirmed"))
        self.assertIn("being sent to Blind, which can't be checked automatically", post["error"])


class Unsure(Findable):
    """Findable whose lookup, while ``unsure`` is set, answers as DEV does when it can't tell: a result naming neither
    URL nor id, with the reason in its notes."""

    def __init__(self):
        super().__init__()
        self.unsure = False

    async def find_existing(self, creds, account, prepared):
        if self.unsure:
            self.lookups += 1
            return PublishResult(url=None, remote_id=None, status="published",
                                 notes=["Findable can't say which post is this one: 2 posts agree with it: id 7, id 8."])
        return await super().find_existing(creds, account, prepared)


class FindingTheSyncNamesTheReason(SyncHarness):
    platform_class = Unsure

    def test_a_push_whose_check_cannot_tell_says_why_and_which_posts_to_look_at(self):
        post_id = self.synced_then_capped()
        self.platform.unsure = True
        result = self.sync()
        self.assertEqual((result.action, result.status, result.post_id), ("skipped", "unconfirmed", post_id))
        self.assertIn("Findable can't say which post is this one: 2 posts agree with it: id 7, id 8", result.message,
                      "the sync said nothing of what the platform found")
        self.assertNotIn("can't be checked automatically", result.message)
        self.assertIn(f"resend post {post_id}", result.message)
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["remote_id"], self.platform.sent), ("unconfirmed", None, []))
        self.assertEqual(self.entry()["status"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
