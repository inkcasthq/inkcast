# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #444 (option B, the owner's decision of 2026-11-04): an old unconfirmed ledger entry with no post id waits for
an edit of the file, on every platform.

Git sync's ledger can hold an entry from before posts were recorded (#228): 'unconfirmed', naming no post, so nothing
can be resent or cancelled. On a platform Inkcast can ask, any push sent the file again once the lookup said "missing":
a late send nobody decided on, and a different rule from a platform it can't ask, where the file waited for an edit.
Now such an entry is treated like any entry waiting for a person's decision: the file is ``skipped`` until it is edited,
on every platform, the message says so, and the edit is published as a new post. A post the platform does have is still
recorded. ``--fail-on-decision``'s help, the action's input and docs/git-publishing.md name the edit for this case.
Each ``Finding`` failed on main; the ``Held`` pins what holds."""

import asyncio
import contextlib
import io
import re
import unittest

from inkcast.cli import sync_main
from inkcast.libs.content.story import parse_story
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.services.publishing.sync import payload_hash

from test_person_decides import PATH, ROOT, SOURCE, Blind, SyncHarness


class OldEntry(SyncHarness):
    """A ledger entry from before posts were recorded: 'unconfirmed', naming no post, for the file as it is."""

    def digest(self, body: str = "Body.") -> str:
        story = parse_story(self.markdown(body), known_platforms=[self.platform.id], schedule_as_written=True)
        return payload_hash(self.platform.prepare(story.for_platform(self.platform.id), story.body))

    def old_entry(self, body: str = "Body.") -> None:
        self.store.put_entry(self.user, SOURCE, PATH, self.platform.id, status="unconfirmed", content_hash=self.digest(body),
                             post_id=None)

    def dry_run(self, body: str = "Body."):
        req = SyncRequest(source=SOURCE, dry_run=True, files=[SyncFile(path=PATH, markdown=self.markdown(body))])
        (result,) = asyncio.run(self.git.sync(self.user, req)).results
        return result

    def assert_waits_for_an_edit(self, result) -> None:
        self.assertEqual((result.action, result.status, result.post_id), ("skipped", "unconfirmed", None))
        self.assertIn("edit this file to publish it as a new post", result.message, "the person is not told what to do")
        self.assertIn("no post of it to resend or cancel", result.message)
        self.assertIn(f"remove the first copy on {self.platform.name} if it did arrive", result.message)
        self.assertNotIn("inkcast_resend", result.message, "a resend of a post that doesn't exist")
        entry = self.entry()
        self.assertEqual((self.platform.sent, self.posts(), entry["status"], entry["post_id"]), ([], [], "unconfirmed", None),
                         "the file was sent late, or the entry moved on")

    def assert_the_edit_is_a_new_post(self) -> None:
        edited = self.sync("Edited.")
        self.assertEqual((edited.action, edited.status), ("create", "published"))
        self.assertEqual(self.platform.sent, ["Hello"])
        entry = self.entry()
        self.assertEqual((entry["status"], entry["post_id"], entry["content_hash"]), ("published", edited.post_id, self.digest("Edited.")))
        self.assertEqual(self.sync("Edited.").action, "unchanged")


class FindingAPlatformThatCanBeAskedNoLongerSendsItLate(OldEntry):
    def test_a_lookup_that_says_missing_waits_for_the_edit_and_the_edit_is_a_new_post(self):
        self.old_entry()
        result = self.sync()                                                  # on main: created and sent, nobody deciding
        self.assert_waits_for_an_edit(result)
        self.assertIn("Findable says it isn't there", result.message)
        self.assertEqual(self.platform.lookups, 1)
        self.assert_waits_for_an_edit(self.sync())                            # every push, until the edit
        self.assertEqual(self.platform.lookups, 2)
        self.assert_the_edit_is_a_new_post()


class FindingAPlatformThatCannotBeAskedSaysTheSame(OldEntry):
    platform_class = Blind

    def test_the_file_waits_for_the_edit_with_the_same_message(self):
        self.old_entry()
        result = self.sync()
        self.assert_waits_for_an_edit(result)
        self.assertIn("Blind can't be checked automatically", result.message)
        self.assert_the_edit_is_a_new_post()


class FindingADryRunSaysWhatTheRealRunDoes(OldEntry):
    def test_an_unedited_file_is_skipped_and_an_edited_one_would_be_a_new_post(self):
        self.old_entry()
        result = self.dry_run()                                               # on main: 'unchanged'
        self.assert_waits_for_an_edit(result)
        self.assertIn("this dry run didn't ask Findable", result.message)
        edited = self.dry_run("Edited.")
        self.assertEqual((edited.action, edited.status), ("create", "preview"))
        self.assertEqual((self.platform.sent, self.posts(), self.entry()["status"], self.platform.lookups),
                         ([], [], "unconfirmed", 0), "a dry run changed something")


class HeldAPostThePlatformHasIsRecorded(OldEntry):
    def test_a_lookup_that_finds_the_post_records_it_on_the_entry(self):
        self.old_entry()
        self.platform.sent.append("Hello")                                    # the first send did arrive
        result = self.sync()
        self.assertEqual((result.action, result.status, result.url), ("unchanged", "published", "https://findable/found"))
        entry = self.entry()
        self.assertEqual((entry["status"], entry["url"], entry["remote_id"], entry["post_id"]),
                         ("published", "https://findable/found", "found", None))
        self.assertEqual(self.posts(), [])


class FindingTheHelpNamesTheEdit(unittest.TestCase):
    def test_fail_on_decision_names_the_edit_in_the_cli_the_action_and_the_docs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            sync_main(["--help"])
        help_text = " ".join(out.getvalue().split())
        option = help_text[help_text.rindex("--fail-on-decision"):].split("--removed-since")[0]
        self.assertIn("edit", option, "the help names no edit for an entry with no post")
        block = re.search(r"\n  fail-on-decision:\n((?:    .*\n)+)", (ROOT / "action" / "action.yml").read_text()).group(1)
        self.assertIn("edit", block, "the action's input names no edit for an entry with no post")
        docs = (ROOT / "docs" / "git-publishing.md").read_text()
        self.assertIn("no post to resend or cancel", docs)
        self.assertIn("until you edit it", docs)


if __name__ == "__main__":
    unittest.main()
