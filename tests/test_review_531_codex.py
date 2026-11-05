# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial checks for repeated note writes in issue #531."""

import unittest

from test_publishing_keep_notes_531 import Sends, copy
from inkcast.libs.pipeline import _both_copies, _edited, _named, _second_copy


class RepeatedSuccessNotes(Sends):
    def test_a_later_attempt_note_is_written_once_across_five_successes(self):
        post_id = self.post("publishing")
        note = _second_copy(self.platform, 2)
        self.store.update_post(post_id, attempts=2, error=note)
        for step in range(1, 6):
            with self.subTest(step=step):
                ((outcome, error),) = self.sends(post_id, copy(1), held=1)
                self.assertEqual(error, note)
                self.assertEqual(outcome.notes, [note])

    def test_a_platform_note_that_matches_a_kept_note_is_written_once(self):
        post_id = self.post("publishing")
        note = _second_copy(self.platform, 2)
        self.store.update_post(post_id, error=note)
        ((outcome, error),) = self.sends(post_id, copy(1, [note]))
        self.assertEqual(error, note)
        self.assertEqual(outcome.notes, [note])

    def test_repeated_conflicting_answers_do_not_repeat_the_same_note(self):
        post_id = self.post("publishing")
        ((_, _),) = self.sends(post_id, copy(1))
        note = _both_copies(self.platform, _named("https://findable/1", "1"),
                            _named("https://findable/9", "9"), 1)
        for step in range(1, 6):
            with self.subTest(step=step):
                ((outcome, error),) = self.sends(post_id, copy(9))
                self.assertEqual(error, note)
                self.assertEqual(outcome.message, note)

    def test_other_row_notes_are_replaced_and_the_kept_note_follows_the_platform_note(self):
        post_id = self.post("publishing")
        kept = _second_copy(self.platform, 2)
        edited = _edited(self.platform, "https://findable/1", "1")
        self.store.update_post(post_id, error=f"person note; timed out; {edited}; {kept}")
        ((outcome, error),) = self.sends(post_id, copy(1, ["platform note"]))
        self.assertEqual(error, f"platform note; {kept}")
        self.assertEqual(outcome.notes, ["platform note", kept])


if __name__ == "__main__":
    unittest.main()
