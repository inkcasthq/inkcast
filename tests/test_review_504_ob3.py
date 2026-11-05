# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The OB3 review of #504: an update keeps every second-copy note a post holds.

``_is_second_copy`` matched each held note against the builders' wording with ``.+`` for every argument. On a note that
starts as a second-copy note and fails to match, the three ``.+`` of ``_both_copies`` backtrack in time cubic in the
note: 1.0 s at 26 KB, 8.1 s at 51 KB, 68 s at 102 KB (measured on bd440c2e), once for every held note, on every pass of
the update's fence loop, which holds the event loop (``Publisher.update``).

A note is one piece of the post's error, which ``notes_of`` splits on "; ". A copy's URL or id is the platform's answer,
taken as it comes (DEV's and Medium's ``data.get("url")``): one that holds "; " split "may have this post twice" into
pieces, none of them a second-copy note, and the update dropped them all. Each ``Finding`` class failed on bd440c2e.

The ``Held`` classes pass on bd440c2e. The first fails on two mutations the PR's tests let through: ``re.search`` for
``re.fullmatch``, and a match that ignores case. The second pins the update's fence loop (#473) around the new rule."""

import time
import unittest
from unittest import mock

from inkcast.libs.pipeline import _both_copies, _is_second_copy, _lost, _named, _second_copy

from test_review_473_ob3 import another_workers_late_success
from test_review_sync_427_456 import TWICE
from test_sync_update_fence_473 import lands_before_the_updates_write
from test_sync_update_second_copy_504 import ATTEMPT_2, TRIMMED, UpdateCase

# A note that begins as Editable's "may have this post twice" and never ends as one: every ", recorded here, and " and
# ", attempt " in it is a place the match is tried again from.
HOSTILE = ("Editable may have this post twice: " + ", recorded here, and x, attempt " * 1600
           + "'s answer. Check Editable: keep one and remove the other!")


class FindingANoteIsRecognisedInTimeLinearInItsLength(UpdateCase):
    def test_a_51_kb_note_that_is_no_second_copy_note_is_told_apart_at_once(self):
        started = time.perf_counter()
        recognised = _is_second_copy(self.platform, HOSTILE)
        took = time.perf_counter() - started
        self.assertFalse(recognised)
        self.assertLess(took, 1.0, f"{len(HOSTILE)} characters took {took:.1f} s to recognise")

    def test_the_update_of_a_post_holding_such_a_note_replaces_it_at_once(self):
        post_id = self.published_with(HOSTILE)
        started = time.perf_counter()
        after = self.edited(post_id)
        took = time.perf_counter() - started
        self.assertEqual(after["error"], "Editable trimmed the tags of the update.")
        self.assertLess(took, 1.0, f"the update took {took:.1f} s")

    def test_every_writers_note_is_still_recognised_whatever_its_arguments_hold(self):
        """The arguments may hold the builders' own words, the separator's neighbours and newlines."""
        p = self.platform
        for arg in ("1", "x, recorded here, and y", ", attempt 2's answer. Check Editable", "a\nb", "\0", "Z" * 5000):
            for note in (_second_copy(p, arg), _both_copies(p, arg, arg, arg), _both_copies(p, "id 1", arg, 3)):
                self.assertTrue(_is_second_copy(p, note), note[:80])
        self.assertTrue(_is_second_copy(p, _lost(p)))


class FindingANoteNamingACopyWhoseUrlHoldsTheSeparatorOutlivesTheUpdate(UpdateCase):
    def test_the_note_is_kept_whole(self):
        """Another worker's success answered with a copy whose URL holds "; ", its note worded as ``_deliver``'s
        ``both_copies`` words it, then an edited sync."""
        first = self.sync()
        row, url = self.store.get_post(first.post_id), "https://editable/9; draft"
        written, _ = self.store.record_success(
            first.post_id, held=row["attempts"], status="published", result_url=url, remote_id="9", sent={}, notes=[],
            second_copy=str, edited="",
            both_copies=lambda before: _both_copies(self.platform, _named(before["result_url"], before["remote_id"]),
                                                    _named(url, "9"), 3))
        note = self.store.get_post(first.post_id)["error"]
        self.assertEqual((written, self.store.notes_of({"error": note})), (False, [note]),
                         "the note naming both copies reads back as more than one note")
        self.assertIn("https://editable/9;%20draft (id 9)", note)
        self.assertEqual(self.edited(first.post_id)["error"], f"{note}; Editable trimmed the tags of the update.")


class HeldANoteWordedOnlyLikeASecondCopyNoteIsReplaced(UpdateCase):
    def test_a_note_quoting_one_or_in_another_case_is_the_platforms(self):
        """Recognised whole and as worded: a platform's note that quotes a second-copy note, or one in another case,
        is the platform's own, and the update replaces it."""
        attempt, lost = _second_copy(self.platform, 2), _lost(self.platform)
        post_id = self.published_with(f"Editable says: {attempt}", f"{attempt} (as Editable words it)", attempt.upper(),
                                      lost.lower(), TWICE.replace("Check Editable", "check Editable"))
        self.assertEqual(self.edited(post_id)["error"], "Editable trimmed the tags of the update.")


class HeldASecondCopyNoteWrittenMeanwhileIsKeptOnceBesideTheHeldOne(UpdateCase):
    def test_the_note_written_between_the_read_and_the_write_follows_the_held_one_once(self):
        """The update's first write misses (#473): another worker's success named another copy after its read. The
        notes are worded again: the held second-copy note, then the new one, once, then the update's own; the platform's
        note of the earlier send is still replaced."""
        post_id = self.published_with(TRIMMED, ATTEMPT_2)
        patched, landed = lands_before_the_updates_write(self, another_workers_late_success(self))
        with mock.patch.object(self.store, "update_post", patched):
            after = self.edited(post_id)
        self.assertEqual([written for written, _ in landed], [False], "the other worker's answer did not land")
        self.assertEqual(after["error"], f"{ATTEMPT_2}; {TWICE}; Editable trimmed the tags of the update.")


if __name__ == "__main__":
    unittest.main()
