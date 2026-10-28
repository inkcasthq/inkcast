# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #213: an error after the post row exists leaves the ledger entry pointing at that post, never 'failed'.

Git sync marked a ledger entry 'failed' whenever ``publish()`` raised, even once the post row existed and the send
may have happened: a ``database is locked`` on the 'published' write after a successful send, and the next sync saw
a 'failed' entry and created the post again, a second send. Now the entry points at the post, with the post's own
status, and that status decides what happens next: 'publishing' is the scheduler's to recover (asked, never
re-sent), 'scheduled' waits for its time, 'unconfirmed' is checked. Only an error before any post row exists marks
the entry 'failed', which the next sync creates afresh. The same on the replace path (an edited scheduled post),
where the claim names no post until its own exists (#220).

The locked write is injected on the one write it is about (``Flaky``), so every case is deterministic. The clock
can't be moved, so the scheduler's recovery pass is given its clock (``run_due(now=...)``)."""

import asyncio
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx2

from inkcast.libs.contracts.models import SyncFile, SyncRequest, SyncResult
from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.services.publishing.sync import GitSync

SOURCE = "github.com/acme/blog"
PATH = "posts/locked.md"


class Findable(Platform):
    """Counts the sends and remembers them; can be asked (``find``, like DEV) whether a payload was published.
    ``error`` answers a send with that error instead."""

    id, name, default_api_url, credential_fields = "findable", "Findable", "https://findable.invalid", []
    capabilities = frozenset({"find"})

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sent: list[str] = []
        self.error: PlatformError | None = None

    async def verify(self, creds):
        return Account(id="1", name=self.id)

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sent.append(prepared.payload["body"].strip())
        if self.error is not None:
            raise self.error
        n = len(self.sent)
        return PublishResult(url=f"https://{self.id}/{n}", remote_id=str(n), status="published")

    async def find_existing(self, creds, account, prepared):
        if prepared.payload["body"].strip() in self.sent:
            n = self.sent.index(prepared.payload["body"].strip()) + 1
            return PublishResult(url=f"https://{self.id}/{n}", remote_id=str(n), status="published")
        return None


class Blind(Findable):
    """The same platform without ``find`` (like Medium): an uncertain send can't be checked automatically."""

    id, name, default_api_url = "blind", "Blind", "https://blind.invalid"
    capabilities = frozenset()


class Flaky:
    """Raises ``sqlite3.OperationalError`` ("database is locked") the first time ``when(*args, **kwargs)`` is true,
    then calls through: the write it stands in for is locked once, as a busy writer elsewhere would lock it."""

    def __init__(self, real, when):
        self.real, self.when, self.raised = real, when, False

    def __call__(self, *args, **kwargs):
        if not self.raised and self.when(*args, **kwargs):
            self.raised = True
            raise sqlite3.OperationalError("database is locked")
        return self.real(*args, **kwargs)


def markdown(platform: str, publish_at: datetime | None = None, body: str = "Body.") -> str:
    when = f"publish_at: {publish_at.isoformat()}\n" if publish_at else ""
    return f"---\ntitle: Locked\nplatforms: [{platform}]\n{when}---\n\n{body}\n"


def at(seconds_from_now: float) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).replace(microsecond=0)


class SyncCase(unittest.TestCase):
    platform_class: type[Findable] = Findable

    def setUp(self):
        self.store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = self.platform_class()
        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "on-call"})
        self.publisher = Publisher(self.store, {self.platform.id: self.platform})
        self.git = GitSync(self.store, {self.platform.id: self.platform}, self.publisher)

    def sync(self, publish_at: datetime | None = None, body: str = "Body.") -> SyncResult:
        req = SyncRequest(source=SOURCE, files=[SyncFile(path=PATH, markdown=markdown(self.platform.id, publish_at, body))])
        (result,) = asyncio.run(self.git.sync(self.user, req)).results
        return result

    def sync_locked(self, locked: Flaky, method: str, publish_at: datetime | None = None, body: str = "Body.") -> None:
        """A sync during which ``method`` of the store is ``locked``: it raises the database error, as it did."""
        with mock.patch.object(self.store, method, locked), self.assertRaises(sqlite3.OperationalError):
            self.sync(publish_at, body)
        self.assertTrue(locked.raised, f"the locked {method} was never reached")

    def entry(self) -> dict:
        return self.store.get_entry(self.user, SOURCE, PATH, self.platform.id)

    def posts(self) -> list[dict]:
        return [dict(r) for r in self.store._all("SELECT * FROM posts ORDER BY rowid")]

    def recover(self) -> None:
        """The scheduler's next pass once the post looks stuck: it asks the platform, and sends only what is missing."""
        asyncio.run(self.publisher.run_due(now=time.time() + STUCK_AFTER + 1))

    def assert_points_at(self, post: dict, status: str) -> None:
        entry = self.entry()
        self.assertEqual((entry["post_id"], entry["status"]), (post["id"], status), "the entry doesn't follow its post")
        self.assertEqual(post["status"], status)


def after_the_send(*args, **kwargs) -> bool:
    """The write of a platform's answer to a send: 'published', or 'unconfirmed' for an uncertain one."""
    return kwargs.get("status") in ("published", "unconfirmed")


def the_post_pointer(*args, **kwargs) -> bool:
    """The ledger write that names the post just made, before anything is sent (``on_created``)."""
    return set(kwargs) == {"post_id", "content_hash"}


def the_entry_itself(*args, **kwargs) -> bool:
    """The ledger write of the sync's outcome, after ``publish()`` returned."""
    return "content_hash" in kwargs and "status" in kwargs


class ANowPostAfterTheRowExists(SyncCase):
    """A file with no ``publish_at`` is sent by the sync itself: the post row is made, then sent, then recorded."""

    def test_a_locked_write_after_the_send_does_not_send_the_post_again(self):
        """The issue's case: the platform took the post, and recording 'published' hit a locked database. The entry
        was marked 'failed', and the next sync created the post again: two sends of one file."""
        self.sync_locked(Flaky(self.store.update_post, after_the_send), "update_post")
        (post,) = self.posts()
        self.assertEqual(self.platform.sent, ["Body."])
        again = self.sync()
        self.assertEqual((self.platform.sent, len(self.posts())), (["Body."], 1), "sent again")
        self.assertEqual((again.action, again.status, again.post_id), ("unchanged", "publishing", post["id"]))
        self.assert_points_at(post, "publishing")
        self.recover()                                                 # the scheduler asks the platform: it is there
        self.assertEqual((self.store.get_post(post["id"])["status"], self.entry()["status"]), ("published", "publishing"))
        settled = self.sync()                                          # and the ledger follows the post
        self.assertEqual((settled.action, settled.status, settled.url), ("unchanged", "published", "https://findable/1"))
        self.assertEqual(self.entry()["remote_id"], "1")
        self.assertEqual((self.platform.sent, len(self.posts())), (["Body."], 1))

    def test_a_locked_write_naming_the_post_leaves_no_row_and_the_next_sync_sends_it_once(self):
        """The ledger write naming the post (before the send) was locked. It is made in the row's own transaction, so
        there is no row: nothing was sent, the entry is 'failed', and the next sync sends the post, once."""
        self.sync_locked(Flaky(self.store.put_entry, the_post_pointer), "put_entry")
        self.assertEqual((self.posts(), self.platform.sent), ([], []))
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("failed", None))
        created = self.sync()
        self.assertEqual((created.action, created.status, self.platform.sent), ("create", "published", ["Body."]))
        self.recover()
        self.assertEqual((self.platform.sent, len(self.posts())), (["Body."], 1))

    def test_a_locked_write_after_publish_returned_still_records_the_post(self):
        """``publish()`` returned 'published', and the ledger write of that outcome was locked. The entry still ends
        on the post, with its URL, not as a claim that holds the file for CLAIM_TTL."""
        self.sync_locked(Flaky(self.store.put_entry, the_entry_itself), "put_entry")
        (post,) = self.posts()
        self.assert_points_at(post, "published")
        self.assertEqual((self.entry()["url"], self.entry()["remote_id"]), ("https://findable/1", "1"))
        again = self.sync()
        self.assertEqual((again.action, again.status, again.url), ("unchanged", "published", "https://findable/1"))
        self.assertEqual(self.platform.sent, ["Body."])


class AnUncertainSendAfterTheRowExists(SyncCase):
    platform_class = Blind

    def test_a_locked_write_of_an_uncertain_answer_is_checked_never_re_sent(self):
        """The platform answered 'may or may not have been published', and recording 'unconfirmed' was locked. The
        post is as uncertain as before: recovery marks it unconfirmed, the next sync asks (and can't tell, on a
        platform without ``find``), and nothing sends it again."""
        self.platform.error = PlatformError("Blind returned a server error (502); it may or may not have been published.",
                                            uncertain=True)
        self.sync_locked(Flaky(self.store.update_post, after_the_send), "update_post")
        self.platform.error = None
        (post,) = self.posts()
        self.assertEqual(self.platform.sent, ["Body."])
        self.assert_points_at(post, "publishing")
        self.assertEqual(self.sync().action, "unchanged")
        self.recover()
        self.assertEqual(self.store.get_post(post["id"])["status"], "unconfirmed")
        checked = self.sync()
        self.assertEqual((checked.action, checked.status), ("skipped", "unconfirmed"))
        self.assertIn("can't be checked automatically", checked.message)
        self.assertEqual((self.platform.sent, len(self.posts())), (["Body."], 1), "sent again")


class AScheduledPostAfterTheRowExists(SyncCase):
    def test_a_locked_write_naming_the_scheduled_post_leaves_no_row_and_the_next_sync_schedules_it(self):
        """A file dated two days ahead, and the ledger write naming its post was locked: no row (it is made in the
        row's own transaction). The next sync schedules it, and the scheduler sends it once, at its time."""
        when = at(2 * 24 * 3600)
        self.sync_locked(Flaky(self.store.put_entry, the_post_pointer), "put_entry", when)
        self.assertEqual(self.posts(), [])
        again = self.sync(when)
        (post,) = self.posts()
        self.assertEqual((again.action, again.status, again.post_id), ("create", "scheduled", post["id"]))
        self.assertAlmostEqual(post["run_at"], when.timestamp(), delta=1)
        self.assertEqual(self.sync(when).action, "unchanged")
        self.assertEqual(len(self.posts()), 1, "scheduled again: two posts at its time")
        asyncio.run(self.publisher.run_due(now=when.timestamp() - 1))
        self.assertEqual(self.platform.sent, [], "sent before its time")
        asyncio.run(self.publisher.run_due(now=when.timestamp() + 1))
        self.assertEqual((self.platform.sent, self.store.get_post(post["id"])["status"]), (["Body."], "published"))
        self.assertEqual((self.sync(when).status, self.entry()["url"]), ("published", "https://findable/1"))


class AReplacementAfterTheRowExists(SyncCase):
    """An edited scheduled post is cancelled and made again. The claim names no post until its own exists (#220), so
    an error once the replacement exists must point the entry at it: the cancelled post can't be followed."""

    def test_a_locked_write_naming_the_replacement_leaves_no_row_and_the_next_sync_makes_it(self):
        when = at(2 * 24 * 3600)
        first = self.sync(when)
        self.sync_locked(Flaky(self.store.put_entry, the_post_pointer), "put_entry", when, body="Better body.")
        (old,) = self.posts()
        self.assertEqual((old["id"], old["status"]), (first.post_id, "cancelled"))
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("failed", None))
        again = self.sync(when, body="Better body.")
        old, new = self.posts()
        self.assertEqual((again.action, again.status, again.post_id), ("create", "scheduled", new["id"]))
        self.assertEqual(new["payload"].count("Better body."), 1)
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "scheduled"], "no twin")
        asyncio.run(self.publisher.run_due(now=when.timestamp() + 1))
        self.assertEqual(self.platform.sent, ["Better body."])

    def test_a_locked_write_after_the_send_of_a_now_replacement_does_not_send_it_again(self):
        """The author removed the ``publish_at``: the replacement is a "now" post, sent by the sync. Recording
        'published' was locked; the next sync follows the post instead of making a third."""
        self.sync(at(2 * 24 * 3600))
        self.sync_locked(Flaky(self.store.update_post, after_the_send), "update_post")
        old, new = self.posts()
        self.assertEqual((old["status"], self.platform.sent), ("cancelled", ["Body."]))
        self.assert_points_at(new, "publishing")
        self.assertEqual(self.sync().action, "unchanged")
        self.assertEqual((self.platform.sent, len(self.posts())), (["Body."], 2), "sent again")
        self.recover()
        self.assertEqual((self.sync().status, self.platform.sent), ("published", ["Body."]))


class BeforeAnyRowExists(SyncCase):
    """An error before the post row exists: nothing was made or sent, so the entry is 'failed' and the next sync
    creates the post, once."""

    def test_the_entry_is_failed_and_the_next_sync_creates_the_post_once(self):
        self.sync_locked(Flaky(self.store.get_credentials, lambda *a, **kw: True), "get_credentials")
        self.assertEqual((self.posts(), self.platform.sent), ([], []))
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("failed", None))
        created = self.sync()
        self.assertEqual((created.action, created.status, self.platform.sent), ("create", "published", ["Body."]))
        self.assertEqual(self.sync().action, "unchanged")
        self.assertEqual(len(self.posts()), 1)

    def test_a_replacement_that_failed_before_its_row_is_made_by_the_next_sync(self):
        """The scheduled post was cancelled for its replacement, and the error came before the replacement's row.
        The claim is over: the entry is 'failed', and the next sync makes the post, not "another sync is in
        progress" until CLAIM_TTL."""
        when = at(2 * 24 * 3600)
        first = self.sync(when)
        self.sync_locked(Flaky(self.store.get_credentials, lambda *a, **kw: True), "get_credentials", when,
                         body="Better body.")
        self.assertEqual((self.store.get_post(first.post_id)["status"], len(self.posts())), ("cancelled", 1))
        self.assertEqual((self.entry()["status"], self.entry()["post_id"]), ("failed", None))
        rerun = self.sync(when, body="Better body.")
        self.assertEqual((rerun.action, rerun.status), ("create", "scheduled"))
        self.assertEqual([p["status"] for p in self.posts()], ["cancelled", "scheduled"])
        self.assertEqual(self.sync(when, body="Better body.").action, "unchanged")
        self.assertEqual(self.store.get_post(first.post_id)["status"], "cancelled", "a cancelled post stays cancelled")


if __name__ == "__main__":
    unittest.main()
