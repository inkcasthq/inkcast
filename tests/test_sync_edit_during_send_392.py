# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #392: an edit made while a send is out is not lost when that send succeeds.

A post the scheduler tried waits for its retry. Its next send is out so long that another worker's stuck pass takes it
over, is told "missing" (the request has not arrived yet) and queues it again at the same attempt count (recovery never
counts a send). The post is 'scheduled' with attempts, so Git sync's edit of the file changes it in place (#204): the
row takes the new content and the entry its hash. The send's success then lands and is written as the platform's truth
(#214). The row read 'published' with content the platform never received, the entry held that content's hash, and the
next sync reported the file unchanged: the edit never reached the platform.

Now a success records what the platform received (``PostsRepo.record_success`` writes the sent title and payload) and
notes an edit it did not carry, and an entry that follows its post into a confirmed status takes the hash of what the
post holds. The next sync sees the edit: sent as an update where the platform edits posts, reported as stale where it
doesn't. ``...IsSentAsAnUpdate`` and ``...IsReportedStale`` failed on main; the rest pin what holds."""

import asyncio
import json
import time
import unittest

from inkcast.libs.content.story import parse_story
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.pipeline import STUCK_AFTER
from inkcast.libs.platforms import PublishResult
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.libs.platforms.medium import MediumPlatform
from inkcast.services.publishing.sync import payload_hash

from test_sync_error_after_post import PATH, SOURCE, Findable, at, markdown
from test_sync_retry_wait_edit import Waiting


class Hooked(Findable):
    """``during_publish(prepared)`` (an async hook) runs inside the next publish while the request is out, before it
    reaches the platform; it unsets itself first."""

    def __init__(self):
        super().__init__()
        self.during_publish = None

    async def publish(self, creds, account, prepared, destination):
        if self.during_publish:
            hook, self.during_publish = self.during_publish, None
            await hook(prepared)
        return await super().publish(creds, account, prepared, destination)


class Editable(Hooked):
    """The same platform that edits posts in place (``update``, like DEV)."""

    id, name, default_api_url = "editable", "Editable", "https://editable.invalid"
    capabilities = frozenset({"find", "update"})

    def __init__(self):
        super().__init__()
        self.updated: list[tuple[str, str]] = []

    async def update(self, creds, account, remote_id, prepared, destination):
        self.updated.append((remote_id, prepared.payload["body"].strip()))
        return PublishResult(url=f"https://{self.id}/{remote_id}", remote_id=remote_id, status="published")


class EditDuringSend(Waiting):
    platform_class = Hooked

    def body_of(self, post: dict) -> str:
        return json.loads(post["payload"])["payload"]["body"].strip()

    async def sync_async(self, publish_at, body: str):
        """One push of the file, from inside the hook (the loop is already running)."""
        req = SyncRequest(source=SOURCE, files=[SyncFile(path=PATH, markdown=markdown(self.platform.id, publish_at, body))])
        (result,) = (await self.git.sync(self.user, req)).results
        return result

    def edit_lands_while_the_send_is_out(self):
        """The waiting post's retry goes out (attempt 2) and is out past STUCK_AFTER: another worker's stuck pass takes
        it over, is told "missing" and queues it again, still at attempt 2; Git sync then edits the file, in place; the
        send's success lands last. ``seen`` records the post as queued again and as edited, and the sync's result."""
        when = at(-600)
        post = self.waiting(when)
        seen: dict = {}

        async def while_a_waits(prepared):                                    # inside A's publish, its request out
            self.platform.sent.clear()                                        # nothing has arrived: the lookup says so
            now = time.time() + STUCK_AFTER + 1
            (stuck,) = self.store.claim_stuck(older_than=now - STUCK_AFTER, now=now)
            await self.publisher.recover(stuck, now)                          # B: missing → queued again, attempt 2
            seen["queued"] = self.store.get_post(post["id"])
            seen["sync"] = await self.sync_async(when, "Edited.")
            seen["edited"] = self.store.get_post(post["id"])

        self.platform.during_publish = while_a_waits
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING") as logged:
            (outcome,) = asyncio.run(self.publisher.run_due(now=post["run_at"] + 1))   # A's tick; its 2xx lands last
        seen["log"] = [r.getMessage() for r in logged.records if getattr(r, "event", None) == "publish.edited_meanwhile"]
        self.assertEqual((seen["queued"]["status"], seen["queued"]["attempts"]), ("scheduled", 2))
        self.assertEqual((seen["sync"].action, seen["sync"].post_id), ("update", post["id"]), "the edit was not in place")
        self.assertEqual((self.body_of(seen["edited"]), self.entry()["content_hash"]),
                         ("Edited.", self.digest("Edited.")), "the edit was not written")
        return post, outcome, seen

    def digest(self, body: str) -> str:
        """The file's hash as the sync computes it (``payload_hash`` of the story rendered for the platform)."""
        story = parse_story(markdown(self.platform.id, at(-600), body), known_platforms=[self.platform.id],
                            schedule_as_written=True)
        return payload_hash(self.platform.prepare(story.for_platform(self.platform.id), story.body))

    def assert_success_recorded_what_was_sent(self, post: dict, outcome) -> dict:
        """The 2xx is the truth (#214): 'published', the copy named, and the content the platform received on the row,
        with the edit it did not carry noted on the row, in the outcome and in the log."""
        now = self.store.get_post(post["id"])
        self.assertEqual((now["status"], now["result_url"], now["remote_id"], now["attempts"]),
                         ("published", f"https://{self.platform.id}/1", "1", 2))
        self.assertEqual(self.body_of(now), "Body.", "the row says the platform has content it never received")
        self.assertEqual(self.platform.sent, ["Body."])
        self.assertIn("changed while this send was out", now["error"] or "")
        self.assertIn(f"{self.platform.name} has it as it was sent", now["error"])
        self.assertEqual((outcome.status, outcome.url, outcome.notes), ("published", now["result_url"], [now["error"]]))
        return now


class FindingTheEditIsSentAsAnUpdate(EditDuringSend):
    platform_class = Editable

    def test_the_next_sync_sends_the_edit_as_an_update_of_the_copy_just_published(self):
        post, outcome, seen = self.edit_lands_while_the_send_is_out()
        now = self.assert_success_recorded_what_was_sent(post, outcome)
        self.assertIn("The next sync sends the change as an update", now["error"])
        self.assertEqual(len(seen["log"]), 1)

        synced = self.sync(at(-600), body="Edited.")
        self.assertEqual((synced.action, synced.status, synced.post_id), ("update", "published", post["id"]),
                         "the edit was reported unchanged and never sent")
        self.assertEqual((self.platform.sent, self.platform.updated), (["Body."], [("1", "Edited.")]))
        entry = self.entry()
        self.assertEqual((entry["status"], entry["content_hash"], entry["remote_id"]), ("published", self.digest("Edited."), "1"))
        self.assertEqual((self.body_of(self.store.get_post(post["id"])), self.store.get_post(post["id"])["error"]), ("Edited.", None))
        self.assertEqual(self.sync(at(-600), body="Edited.").action, "unchanged")
        self.assertEqual(self.platform.updated, [("1", "Edited.")], "sent again")

    def test_a_file_put_back_as_it_was_sent_is_unchanged(self):
        """The entry's hash is what the platform has, so the edit reverted before the next sync is no change."""
        post, outcome, _ = self.edit_lands_while_the_send_is_out()
        self.assert_success_recorded_what_was_sent(post, outcome)
        reverted = self.sync(at(-600), body="Body.")
        self.assertEqual((reverted.action, reverted.status), ("unchanged", "published"))
        self.assertEqual(self.platform.updated, [])
        self.assertEqual(self.entry()["content_hash"], self.digest("Body."))


class FindingTheEditIsReportedStale(EditDuringSend):
    """A platform that doesn't edit posts (like Medium): the person is told, and nothing is posted again."""

    def test_the_next_sync_reports_the_file_stale_and_names_the_copy(self):
        post, outcome, _ = self.edit_lands_while_the_send_is_out()
        now = self.assert_success_recorded_what_was_sent(post, outcome)
        self.assertIn(f"{self.platform.name} doesn't let Inkcast edit posts: update it by hand: {now['result_url']}", now["error"])

        synced = self.sync(at(-600), body="Edited.")
        self.assertEqual((synced.action, synced.status, synced.url), ("stale", "published", now["result_url"]),
                         "the edit was reported unchanged")
        self.assertIn(f"Update it by hand: {now['result_url']}", synced.message)
        self.assertEqual(self.platform.sent, ["Body."], "posted again")
        self.assertEqual(self.entry()["message"], synced.message)
        self.assertEqual(self.sync(at(-600), body="Edited.").action, "stale")


class HeldASuccessForTheContentThePostHolds(EditDuringSend):
    def test_a_send_nobody_edited_records_no_note(self):
        """The usual retry: the row holds what was sent, so nothing is noted and the next sync is unchanged."""
        when = at(-600)
        post = self.waiting(when)
        (outcome,) = asyncio.run(self.publisher.run_due(now=post["run_at"] + 1))
        now = self.store.get_post(post["id"])
        self.assertEqual((now["status"], now["error"], outcome.notes, self.body_of(now)), ("published", None, [], "Body."))
        self.assertEqual(self.sync(when).action, "unchanged")
        self.assertEqual(self.entry()["content_hash"], self.digest("Body."))

    def test_a_stored_payload_hashes_as_the_prepared_it_came_from(self):
        """The entry's hash from the row (``stored_hash``) is ``payload_hash`` of the prepared the row stores, through
        JSON, for the built-in plug-ins' output: an unchanged file stays unchanged once its post is followed."""
        from inkcast.services.publishing.sync import stored_hash   # the fix's helper: absent on main, where the rest still runs
        fields = {"title": "Locked", "subtitle": "Sub", "tags": ["a", "B c"], "canonical_url": "https://blog.example/locked",
                  "draft": False, "destination": "7"}
        for platform in (DevtoPlatform(None), MediumPlatform(None), self.platform):
            with self.subTest(platform=platform.id):
                prepared = platform.prepare(dict(fields), "Body.")
                self.assertEqual(stored_hash(json.loads(json.dumps(prepared.to_dict()))), payload_hash(prepared))


if __name__ == "__main__":
    unittest.main()
