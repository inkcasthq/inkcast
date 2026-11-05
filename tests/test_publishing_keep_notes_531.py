# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #531: a send's success of the copy a post already records keeps every note that the platform may have the
post twice.

``PostsRepo.record_success`` carried one such note onto a written success, ``_lost`` (#234). The two others a success
writes when the platform may have the post twice (#214) went with the next success of the copy the row records:
another worker's attempt that may have arrived too (``_second_copy``), and "may have this post twice", naming both
copies (``_both_copies``). Such a success is no person's decision, and only a person clears that note (#234), as an
update keeps it since #504; the same recogniser decides (``_is_second_copy``). The ``Finding`` classes failed on
058a325c; the ``Held`` classes pin what holds around them: ``_lost`` is carried as before, and the edited-while-out note
(``_edited``, #392) is the success's own, as before."""

import asyncio
import json
import time
import unittest
from dataclasses import dataclass, field

import test_review_publishing_214 as r214
from inkcast.libs.pipeline import STUCK_AFTER, Publisher, _both_copies, _edited, _lost, _named, _second_copy
from inkcast.libs.platforms import PlatformError, PublishResult

TRIMMED = "Findable trimmed the tags to four."                       # a platform's own note on a send


def copy(n: int, notes: list[str] | None = None) -> PublishResult:
    """Findable's answer naming copy ``n``, as its ``publish`` names its copies."""
    return PublishResult(url=f"https://findable/{n}", remote_id=str(n), status="published", notes=notes or [])


class Sends(r214.Harness):
    def sends(self, post_id: str, *answers: PublishResult | PlatformError, held: int = 0) -> list[tuple]:
        """Each answer in turn as a send of ``post_id`` holding it at ``held`` (``Publisher._send``, the scheduler's own
        call, so the pipeline words the notes and passes its recogniser); ``(outcome, the post's error after)`` each."""
        prepared = self.platform.prepare({"title": "t"}, "Body.")
        out = []
        for answer in answers:
            self.platform.answer = answer
            outcome = asyncio.run(self.publisher._send(post_id, self.user, self.platform, prepared, attempts=held or 1,
                                                       held=held))
            out.append((outcome, self.store.get_post(post_id)["error"]))
        return out

    def changed(self, post_id: str) -> None:
        """The row comes to hold other content than a send carried: an author's edit while it was out (#392)."""
        other = self.platform.prepare({"title": "t"}, "Edited.").to_dict()
        self.store.update_post(post_id, payload=json.dumps(other))


# ---- a success of the copy recorded keeps every note that the platform may have the post twice ----------------------


class FindingASuccessOfTheCopyRecordedKeepsTheNoteNamingBothCopies(Sends):
    def test_the_issues_sequence(self):
        """A success naming copy 1; one naming copy 9, refused, which notes both; a late success naming copy 1, the copy
        recorded. On 058a325c the third wrote ``error=None``."""
        post_id = self.post("publishing")
        (first, after_1), (second, after_2), (third, after_3) = self.sends(post_id, copy(1), copy(9), copy(1))
        twice = _both_copies(self.platform, _named("https://findable/1", "1"), _named("https://findable/9", "9"), 1)
        self.assertEqual((first.status, first.url, after_1), ("published", "https://findable/1", None))
        self.assertEqual((second.status, second.url, second.message, after_2),
                         ("published", "https://findable/1", twice, twice), "copy 9 was not refused with both named")
        post = self.store.get_post(post_id)
        self.assertEqual((third.status, post["status"], post["result_url"], post["remote_id"]),
                         ("published", "published", "https://findable/1", "1"))
        self.assertEqual(after_3, twice, "the success of the copy recorded dropped the note naming both copies")
        self.assertEqual(third.notes, [twice], "the caller is not told what the post says")

    def test_the_note_comes_before_the_successes_own_edited_note(self):
        """The row's notes as ``record_success`` words them: the platform's, the notes carried over, another attempt's,
        then the edited-while-out note."""
        post_id = self.post("publishing")
        (_, _), (_, twice) = self.sends(post_id, copy(1), copy(9))
        self.changed(post_id)
        ((outcome, error),) = self.sends(post_id, copy(1, [TRIMMED]))
        edited = _edited(self.platform, "https://findable/1", "1")
        self.assertEqual(error, f"{TRIMMED}; {twice}; {edited}")
        self.assertEqual(outcome.notes, [TRIMMED, twice, edited])


class FindingASuccessOfTheCopyRecordedKeepsTheNoteThatAnotherAttemptMayHaveArrived(Sends):
    def test_the_re_sends_answer_naming_the_copy_recorded_keeps_it(self):
        """Worker A's success (attempt 1) lands while another worker's re-send (attempt 2) is out: written, with the note
        that attempt 2 may have arrived too. The re-send's own success then names the copy recorded (a platform that
        answers a repeated request with the post it made). On 058a325c it wrote ``error=None``."""
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=2)                           # another worker's re-send is out
        ((a, after_a),) = self.sends(post_id, copy(1), held=1)
        attempt_2 = _second_copy(self.platform, 2)
        self.assertEqual((a.status, after_a), ("published", attempt_2))
        ((b, after_b),) = self.sends(post_id, copy(1), held=2)
        self.assertEqual((b.status, b.url), ("published", "https://findable/1"))
        self.assertEqual(after_b, attempt_2, "the success of the copy recorded dropped the note of another attempt")
        self.assertEqual(b.notes, [attempt_2])


@dataclass
class Gate:
    """One send's two holds (``Gated``): before its request reaches the platform, and before its answer comes back."""

    began: asyncio.Event = field(default_factory=asyncio.Event)
    arrive: asyncio.Event = field(default_factory=asyncio.Event)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    answer: asyncio.Event = field(default_factory=asyncio.Event)


class Gated(r214.Findable):
    """Findable whose sends take ``gates`` in the order they begin: each request waits for ``arrive`` before it reaches
    the platform (a lookup finds the copy from then), and its answer for ``answer`` after."""

    def __init__(self):
        super().__init__()
        self.gates: list[Gate] = []

    async def publish(self, creds, account, prepared, destination):
        gate = self.gates.pop(0)
        gate.began.set()
        await gate.arrive.wait()
        self.sent.append(prepared.title)
        n = len(self.sent)
        gate.arrived.set()
        await gate.answer.wait()
        return copy(n)


class FindingWorkersReachASuccessOfTheCopyRecorded(r214.Harness):
    """The sequence through the scheduler itself, three workers on their own connections, nothing written by hand."""

    def setUp(self):
        super().setUp()
        self.platform = Gated()
        self.platforms[self.platform.id] = self.platform
        self.publisher = Publisher(self.store, self.platforms)

    def test_a_lookups_match_then_another_copys_success_then_the_first_copys_own_answer(self):
        """Worker A sends (attempt 1). Past STUCK_AFTER, worker B's stuck pass is told "missing" (A's request has not
        arrived) and re-sends (attempt 2). Both requests then arrive, copies 1 and 2, their answers held. Past
        STUCK_AFTER again, worker C's lookup finds copy 1 and records it. B's success names copy 2: refused, both named.
        A's success names copy 1, the copy recorded: on 058a325c it replaced the note naming copy 2 with "attempt 2 may
        have arrived too", and nothing on the post said where the second copy is."""
        post_id = self.post()
        b, c = self.replica(), self.replica()
        seen = []

        async def run():
            a_gate, b_gate = Gate(), Gate()
            self.platform.gates = [a_gate, b_gate]
            a_tick = asyncio.create_task(self.publisher.run_due(now=time.time()))
            await a_gate.began.wait()
            b_tick = asyncio.create_task(b.run_due(now=time.time() + STUCK_AFTER + 1))   # missing → re-queued → sending
            await b_gate.began.wait()
            for gate in (a_gate, b_gate):                                     # A's request arrives, then B's
                gate.arrive.set()
                await gate.arrived.wait()
            (found,) = await c.run_due(now=time.time() + STUCK_AFTER + 1)     # C's lookup finds copy 1
            seen.append(("c", found.status, found.url, self.store.get_post(post_id)["error"]))
            b_gate.answer.set()
            requeued, b_outcome = await b_tick
            seen.append(("b", requeued.status, b_outcome.status, b_outcome.url, self.store.get_post(post_id)["error"]))
            a_gate.answer.set()
            (a_outcome,) = await a_tick
            return a_outcome

        a_outcome = asyncio.run(run())
        twice = _both_copies(self.platform, _named("https://findable/1", "1"), _named("https://findable/2", "2"), 2)
        attempt_2 = _second_copy(self.platform, 2)
        self.assertEqual(seen, [("c", "published", "https://findable/1", None),
                                ("b", "scheduled", "published", "https://findable/1", twice)])
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["result_url"], post["attempts"], self.platform.sent),
                         ("published", "https://findable/1", 2, ["t", "t"]))
        self.assertEqual(post["error"], f"{twice}; {attempt_2}", "A's success dropped the note naming copy 2")
        self.assertEqual(a_outcome.notes, [twice, attempt_2])


class FindingRecordSuccessKeepsTheNotesItsCallerNames(r214.Harness):
    def test_the_notes_kept_follow_the_successes_own_in_the_rows_order(self):
        """The db layer's part, with the repro's stand-in wording (``same_copy504.py``): ``keeps`` comes from the caller,
        so the store knows no note's wording."""
        post_id = self.post("publishing")
        sent = json.loads(self.store.get_post(post_id)["payload"])
        self.store.update_post(post_id, attempts=2, error="P; TWICE; Q; LOST")
        written, _ = self.store.record_success(
            post_id, held=1, status="published", result_url="u/1", remote_id="1", sent=dict(sent, title="other"),
            notes=["N"], second_copy=lambda a: f"ATTEMPT {a}", both_copies=lambda _row: "BOTH", edited="EDITED",
            keeps=lambda note: note in ("LOST", "TWICE"))
        self.assertTrue(written)
        self.assertEqual(self.store.get_post(post_id)["error"], "N; TWICE; LOST; ATTEMPT 2; EDITED")

    def test_without_keeps_none_is_carried(self):
        post_id = self.post("publishing")
        sent = json.loads(self.store.get_post(post_id)["payload"])
        self.store.update_post(post_id, error="TWICE")
        written, _ = self.store.record_success(
            post_id, held=0, status="published", result_url="u/1", remote_id="1", sent=sent, notes=[],
            second_copy=str, both_copies=lambda _row: "BOTH", edited="EDITED")
        self.assertEqual((written, self.store.get_post(post_id)["error"]), (True, None))


# ---- what holds -----------------------------------------------------------------------------------------------------


class HeldTheLostNoteIsCarriedAsBefore(Sends):
    def test_a_re_sends_success_after_an_earlier_sends_lost_answer_keeps_it_and_logs_it(self):
        """#234: worker A's uncertain answer lands over another worker's re-send (attempt 2), noting that it may have
        arrived too; the re-send's success then keeps that note after its own, tells the caller, and logs it."""
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=2)
        lost = _lost(self.platform)
        ((a, after_a),) = self.sends(post_id, PlatformError("Findable timed out after the request was sent.",
                                                            uncertain=True), held=1)
        self.assertEqual((a.status, after_a), ("unconfirmed", f"Findable timed out after the request was sent.; {lost}"))
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING") as logged:
            ((b, after_b),) = self.sends(post_id, copy(1, [TRIMMED]), held=2)
        self.assertEqual((b.status, after_b, b.notes), ("published", f"{TRIMMED}; {lost}", [TRIMMED, lost]))
        self.assertEqual([m for m in self.second_copies(logged) if "was published by a re-send" in m],
                         [f"Post {post_id} on findable was published by a re-send; an earlier send's answer was lost "
                          f"after its request was sent, and may have posted a second copy"])


class HeldTheEditedNoteIsTheSuccessesOwn(Sends):
    def test_a_success_over_other_content_notes_the_edit(self):
        post_id = self.post("publishing")
        self.changed(post_id)
        ((outcome, error),) = self.sends(post_id, copy(1))
        edited = _edited(self.platform, "https://findable/1", "1")
        self.assertEqual((outcome.status, error, outcome.notes), ("published", edited, [edited]))

    def test_an_edited_note_the_row_holds_is_not_carried_by_a_success_of_the_content_it_holds(self):
        """The note is worded and settled from the content (#392, #427), not a note that the platform may have the post
        twice: a success whose content the row holds writes its own notes, as before."""
        post_id = self.post("publishing")
        self.changed(post_id)
        ((_, edited),) = self.sends(post_id, copy(1))
        self.assertEqual(edited, _edited(self.platform, "https://findable/1", "1"))
        ((outcome, error),) = self.sends(post_id, copy(1))                   # the row now holds what was sent
        self.assertEqual((outcome.status, error, outcome.notes), ("published", None, []))


if __name__ == "__main__":
    unittest.main()
