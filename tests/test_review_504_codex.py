# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #504 (Codex): a second-copy note is recognised exactly, in linear time, and an update keeps such notes in
order, through the #473 fence too."""

import itertools
import re
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from inkcast.libs.pipeline import _both_copies, _edited, _is_second_copy, _lost, _named, _second_copy, _undecryptable
from test_sync_update_fence_473 import lands_before_the_updates_write
from test_sync_update_second_copy_504 import UpdateCase


def by_pattern(platform, note: str) -> bool:
    """The recogniser #504 was first built with: each builder's wording, every argument ``.+``, matched whole."""
    words = (_lost(platform), _second_copy(platform, "\0"), _both_copies(platform, "\0", "\0", "\0"))
    return any(re.fullmatch(".+".join(map(re.escape, w.split("\0"))), note, re.DOTALL) for w in words)


class ExactRecognition(unittest.TestCase):
    def test_builders_match_the_old_closures_byte_for_byte(self):
        cases = (
            ("Editable", "https://example/a", "one", 2),
            ("a.*[x]", "https://example/a?x=[1].*", "id.*[x]", 19),
            ("A\0B", "https://example/a\0b", "one\0two", "3\0x"),
            ("A\nB", "https://example/a\nb", "one\ntwo", "4\n5"),
            ("A; B", "https://example/a; b", "one; two", "5; x"),
            ("A" * 100000, "https://example/" + "x" * 100000, "i" * 100000, "7" * 100000),
        )
        for name, url, remote_id, attempt in cases:
            platform = SimpleNamespace(name=name)
            recorded = _named(url, remote_id)
            answered = _named(url + "/new", remote_id + "-new")
            old_one = (f"Attempt {attempt} was sent by another worker while this one was out, and may have arrived too: a "
                       f"second copy may be on {name}. Check {name}, and remove the extra copy there if it is.")
            old_both = (f"{name} may have this post twice: {recorded}, recorded here, and {answered}, attempt {attempt}'s "
                        f"answer. Check {name}: keep one and remove the other.")
            self.assertEqual(_second_copy(platform, attempt).encode(), old_one.encode())
            self.assertEqual(_both_copies(platform, recorded, answered, attempt).encode(), old_both.encode())
            self.assertTrue(_is_second_copy(platform, old_one))
            self.assertTrue(_is_second_copy(platform, old_both))

    def test_nul_in_name_does_not_match_another_platform(self):
        platform = SimpleNamespace(name="A\0B")
        other = SimpleNamespace(name="AXB")
        self.assertTrue(_is_second_copy(platform, _second_copy(platform, 2)))
        self.assertFalse(_is_second_copy(platform, _second_copy(other, 2)))
        self.assertFalse(_is_second_copy(platform, _both_copies(other, "id 1", "id 2", 2)))
        self.assertTrue(_is_second_copy(platform, _lost(platform)))

    def test_a_note_with_extra_text_or_changed_case_is_not_a_writer_note(self):
        platform = SimpleNamespace(name="Editable")
        note = _second_copy(platform, 2)
        self.assertFalse(_is_second_copy(platform, f"platform says: {note}"))
        self.assertFalse(_is_second_copy(platform, f"{note} Check again."))
        self.assertFalse(_is_second_copy(platform, note.lower()))

    def test_other_notes_are_not_recognized(self):
        platform = SimpleNamespace(name="Editable", capabilities=frozenset({"update"}))
        other = SimpleNamespace(name="Other")
        notes = (
            "Editable trimmed tags.", _edited(platform, "https://editable/1", "1"), _undecryptable(),
            "Cancelled while Editable had not said whether it arrived: it may already be on Editable.",
            "The person resent this post to Editable.", _lost(other), _second_copy(other, 2),
            _both_copies(other, "id 1", "id 2", 2),
        )
        self.assertTrue(all(not _is_second_copy(platform, note) for note in notes))

    def test_it_agrees_with_the_pattern_on_every_short_note(self):
        """Every note built from the wording's own pieces and short arguments, the empty ones and the edge where a later
        separator would leave the attempt empty included: the linear reading answers as the pattern does."""
        platform = SimpleNamespace(name="Ed")
        pieces = ["", "x", ", recorded here, and ", ", attempt ", "'s answer"]
        head, tail = "Ed may have this post twice: ", "'s answer. Check Ed: keep one and remove the other."
        checked = 0
        for n in range(5):
            for parts in itertools.product(pieces, repeat=n):
                for note in (head + "".join(parts) + tail, "Attempt " + "".join(parts) + _second_copy(platform, "")[8:]):
                    self.assertEqual(_is_second_copy(platform, note), by_pattern(platform, note), repr(note))
                    checked += 1
        self.assertGreater(checked, 1000)

    def test_a_long_platform_note_is_read_in_linear_time(self):
        """53 KB of the wording's separators: the pattern took 8 s here (cubic: 1 s at 26 KB); this takes microseconds."""
        platform = SimpleNamespace(name="Editable")
        note = "Editable may have this post twice: " + ", recorded here, and x, attempt y" * 1600
        started = time.perf_counter()
        self.assertFalse(_is_second_copy(platform, note))
        self.assertLess(time.perf_counter() - started, 0.5)


class HeldNotesOfAnUpdate(UpdateCase):
    def test_held_duplicates_stay_in_order(self):
        note = _second_copy(self.platform, 2)
        post_id = self.published_with(note, note)
        self.assertEqual(self.edited(post_id)["error"],
                         f"{note}; {note}; Editable trimmed the tags of the update.")

    def test_fence_keeps_a_held_note_and_a_later_second_copy_note(self):
        held = _second_copy(self.platform, 2)
        later = _both_copies(self.platform, "id 1", "id 9", 3)
        post_id = self.published_with(held)

        def other_worker(post_id):
            row = self.store.get_post(post_id)
            return self.store.update_post(post_id, error=f"{row['error']}; {later}")

        patched, landed = lands_before_the_updates_write(self, other_worker)
        with mock.patch.object(self.store, "update_post", patched):
            after = self.edited(post_id)
        self.assertEqual(landed, [True])
        self.assertEqual(after["error"], f"{held}; {later}; Editable trimmed the tags of the update.")


if __name__ == "__main__":
    unittest.main()
