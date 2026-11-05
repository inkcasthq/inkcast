# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of PR #555 (#531): a success that carries the row's second-copy notes writes each of them once.

``PostsRepo.record_success(keeps=)`` carries every note of the row that ``keeps`` names, then adds another attempt's note
when a later send may have arrived (``may_have_arrived_since``). Both lists can name the same note, and the success
wrote it twice, then once more at each such success: the row's error and the ``Outcome.notes`` grew without bound."""

import test_publishing_keep_notes_531 as t
from inkcast.libs.pipeline import _both_copies, _named, _second_copy


class FindingASuccessWritesAnotherAttemptsNoteOnce(t.Sends):
    """A success of an earlier send (``held`` below ``attempts``) of the copy recorded: the row already holds "Attempt
    N may have arrived too" from the first success, ``keeps`` carries it, and ``second_copy(attempts)`` words it again.
    On c4df1cbf the error held it twice."""

    def test_two_earlier_sends_answered_with_the_copy_recorded(self):
        """Two re-sends out (attempts 3), on a platform that answers a repeated request with the post it made, the
        premise of ``test_the_re_sends_answer_naming_the_copy_recorded_keeps_it``. 058a325c wrote the note once."""
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=3)
        attempt_3 = _second_copy(self.platform, 3)
        ((a, after_a),) = self.sends(post_id, t.copy(1), held=1)
        self.assertEqual((a.status, after_a, a.notes), ("published", attempt_3, [attempt_3]))
        ((b, after_b),) = self.sends(post_id, t.copy(1), held=2)
        self.assertEqual((b.status, after_b), ("published", attempt_3), "attempt 3's note was written twice")
        self.assertEqual(b.notes, [attempt_3], "the caller was told attempt 3's note twice")

    def test_the_error_does_not_grow_with_each_success(self):
        """Attempt 2's note, then both copies named, then three more successes of an earlier send of the copy recorded:
        the error stays as the row holds it, in its order."""
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=2)
        attempt_2 = _second_copy(self.platform, 2)
        twice = _both_copies(self.platform, _named("https://findable/1", "1"), _named("https://findable/9", "9"), 1)
        (_, (_, after_9)) = self.sends(post_id, t.copy(1), t.copy(9), held=1)
        self.assertEqual(after_9, f"{attempt_2}; {twice}")
        for (outcome, error) in self.sends(post_id, t.copy(1), t.copy(1), t.copy(1), held=1):
            self.assertEqual(error, f"{attempt_2}; {twice}", "the error grew at a success of the copy recorded")
            self.assertEqual(outcome.notes, [attempt_2, twice])


class FindingASuccessWritesItsOwnNoteOnce(t.Sends):
    """A platform's own note in the wording ``keeps`` recognises (``_is_second_copy`` matches any non-empty argument):
    the next success's notes hold it, ``keeps`` carries the row's copy of it too. On c4df1cbf the error held it once
    more at each success; 058a325c carried only ``_lost`` and wrote it once."""

    def test_each_success_writes_the_platforms_note_once(self):
        post_id = self.post("publishing")
        shaped = _second_copy(self.platform, "1 of the scheduled batch")
        for (outcome, error) in self.sends(post_id, t.copy(1, [shaped]), t.copy(1, [shaped]), t.copy(1, [shaped])):
            self.assertEqual(error, shaped, "the success wrote its own note again")
            self.assertEqual(outcome.notes, [shaped])
