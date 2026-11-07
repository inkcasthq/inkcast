# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #559: an uncertain answer over a post that records a confirmed copy keeps the copy and its notes.

An earlier send's uncertain answer (a timeout after its request went out, a 2xx that names nothing) and a plug-in's crash
while sending were written by one conditional ``update_post``. Over a post a later attempt had already confirmed, that
write moved it from 'published' to 'unconfirmed', although the row names a copy the platform confirmed, and a person was
asked to check or resend it: a third copy. It also replaced the row's notes with the answer and ``_lost``, so a note
naming two copies (``_both_copies``, #504) was gone, which only a person clears (#234).

Now the answer leaves the status, the copy and every note where they are, and adds ``_lost`` once
(``PostsRepo.record_uncertain``); the caller is told what the post is, with its notes (``Publisher._uncertain``). The
``Finding`` classes failed on f5869458. The ``Held`` classes passed there too, and pin what holds around them: an answer
over a row with no confirmed copy, and the two refusals (a cancel, #216; a copy found at this very attempt, #226)."""

import asyncio
import sqlite3
import time
import unittest
from dataclasses import dataclass

import test_person_decides as pd
import test_publishing_keep_notes_531 as k531
import test_review_publishing_214 as r214
from inkcast.libs.contracts.models import PublishRequest
from inkcast.libs.observability import Metrics
from inkcast.libs.pipeline import STUCK_AFTER, Publisher, _both_copies, _edited, _lost, _named
from inkcast.libs.platforms import PlatformError, PublishResult
from inkcast.services.publishing.mcp_tools import render_publish

TIMEOUT = "Findable timed out after the request was sent."
TRIMMED = k531.TRIMMED
URL = "https://findable/1"


def timeout() -> PlatformError:
    return PlatformError(TIMEOUT, uncertain=True)


def nameless() -> PublishResult:
    """A 2xx that names neither the URL nor the id: ``_deliver`` treats it as a lost answer (#214)."""
    return PublishResult(url=None, remote_id=None, status="published")


class OddError(PlatformError):
    """A plug-in's error whose constructor skips the base: the pipeline can't read its flags, so ``_send`` hands it to
    ``_crashed`` (#174)."""

    def __init__(self):
        Exception.__init__(self, "odd")


class Recorded(k531.Sends):
    """A post another worker's re-send (attempt 2) confirmed, while worker A's send (attempt 1) is still out."""

    def recorded(self, answer: PublishResult | None = None) -> str:
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=2)                           # the re-send's claim
        self.sends(post_id, answer or k531.copy(1), held=2)
        return post_id

    def measured(self) -> None:
        self.publisher.metrics = Metrics()
        self.publisher.metrics.counter("inkcast_publish_total", "")

    def counted(self, status: str = "unconfirmed") -> float:
        counts = self.publisher.metrics.snapshot().get("inkcast_publish_total", {})
        return counts.get((("platform", "findable"), ("status", status)), 0)

    def events(self, post_id: str) -> list[str]:
        return [e.type for e in self.store.poll("audit", limit=200) if e.subject == post_id]

    def shows(self, post_id: str) -> tuple:
        post = self.store.get_post(post_id)
        return post["status"], post["result_url"], post["remote_id"], post["error"]


# ---- the issue's sequence, through the scheduler ----------------------------------------------------------------------


@dataclass
class Holds(k531.Gate):
    """A send's gate whose held answer is an error (``raises``), raised once its request has reached the platform."""

    raises: Exception | None = None


class Answering(k531.Gated):
    def __init__(self):
        super().__init__()
        self.gates: list[Holds] = []

    async def publish(self, creds, account, prepared, destination):
        gate = self.gates[0]
        answer = await super().publish(creds, account, prepared, destination)
        if gate.raises is not None:
            raise gate.raises
        return answer


class FindingWorkersAnUncertainAnswerKeepsTheCopyRecorded(r214.Harness):
    """Three workers on their own connections, nothing written by hand. Worker A sends (attempt 1); past STUCK_AFTER,
    worker B's stuck pass is told "missing" and re-sends (attempt 2); both answers are held. The ``found`` worker's
    request reaches Findable first, copy 1, and worker C's lookup finds it and records it; the other's arrives after,
    copy 2. Then the answers land in the order ``landing`` says, A's being ``a_answer``."""

    def setUp(self):
        super().setUp()
        self.platform = Answering()
        self.platforms[self.platform.id] = self.platform
        self.publisher = Publisher(self.store, self.platforms)

    def three_workers(self, found: str, landing: tuple[str, str], a_answer: Exception) -> list[tuple]:
        """Per answer in the order it landed: the worker, what its caller was told, and the post after it."""
        post_id = self.post()
        b, c = self.replica(), self.replica()
        other = "b" if found == "a" else "a"
        seen = []

        async def run():
            gates = {"a": Holds(raises=a_answer), "b": Holds()}
            self.platform.gates = [gates["a"], gates["b"]]
            ticks = {"a": asyncio.create_task(self.publisher.run_due(now=time.time()))}
            await gates["a"].began.wait()
            ticks["b"] = asyncio.create_task(b.run_due(now=time.time() + STUCK_AFTER + 1))  # missing → re-queued → sending
            await gates["b"].began.wait()
            gates[found].arrive.set()
            await gates[found].arrived.wait()
            await c.run_due(now=time.time() + STUCK_AFTER + 1)                # C's lookup finds copy 1
            gates[other].arrive.set()
            await gates[other].arrived.wait()
            for worker in landing:
                gates[worker].answer.set()
                told = (await ticks[worker])[-1]
                post = self.store.get_post(post_id)
                seen.append((worker, (told.status, told.url, told.remote_id, told.notes),
                             (post["status"], post["result_url"], post["remote_id"], post["attempts"], post["error"])))

        asyncio.run(run())
        self.assertEqual(self.platform.sent, ["t", "t"])
        return seen

    def twice(self, attempt: int) -> str:
        return _both_copies(self.platform, _named(URL, "1"), _named("https://findable/2", "2"), attempt)

    def test_the_issues_sequence_a_timeout_lands_last(self):
        """On 058a325c and f5869458 A's answer moved the post to 'unconfirmed' and dropped the note naming copy 2."""
        seen = self.three_workers("a", ("b", "a"), timeout())
        twice, lost = self.twice(2), _lost(self.platform)
        self.assertEqual(seen[0][2], ("published", URL, "1", 2, twice))
        self.assertEqual(seen[1], ("a", ("published", URL, "1", [twice, lost]), ("published", URL, "1", 2, f"{twice}; {lost}")))

    def test_a_timeout_lands_first_and_the_note_naming_both_copies_follows(self):
        seen = self.three_workers("a", ("a", "b"), timeout())
        twice, lost = self.twice(2), _lost(self.platform)
        self.assertEqual(seen[0], ("a", ("published", URL, "1", [lost]), ("published", URL, "1", 2, lost)))
        self.assertEqual(seen[1][2], ("published", URL, "1", 2, f"{lost}; {twice}"))

    def test_a_timeout_over_the_re_sends_copy_and_the_re_sends_success_keeps_the_note(self):
        """The copy recorded is the re-send's; A's request arrived too, as copy 2, and only a timeout says so."""
        seen = self.three_workers("b", ("a", "b"), timeout())
        lost = _lost(self.platform)
        self.assertEqual(seen[0], ("a", ("published", URL, "1", [lost]), ("published", URL, "1", 2, lost)))
        self.assertEqual(seen[1], ("b", ("published", URL, "1", [lost]), ("published", URL, "1", 2, lost)))

    def test_a_plug_ins_crash_lands_last(self):
        """``_crashed`` made the same write, with the same two defects."""
        seen = self.three_workers("a", ("b", "a"), RuntimeError("boom"))
        twice, lost = self.twice(2), _lost(self.platform)
        self.assertEqual(seen[1], ("a", ("published", URL, "1", [twice, lost]), ("published", URL, "1", 2, f"{twice}; {lost}")))


# ---- what the row keeps -----------------------------------------------------------------------------------------------


class FindingEveryUncertainAnswerKeepsTheCopy(Recorded):
    def test_each_way_a_send_can_end_uncertain(self):
        lost = _lost(self.platform)
        for name, answer in (("a timeout", timeout()), ("a 2xx that names nothing", nameless()),
                             ("a plug-in's crash", RuntimeError("boom")), ("an error the pipeline can't read", OddError())):
            with self.subTest(name):
                post_id = self.recorded()
                ((told, _),) = self.sends(post_id, answer, held=1)
                self.assertEqual(self.shows(post_id), ("published", URL, "1", lost))
                self.assertEqual((told.status, told.url, told.remote_id, told.notes), ("published", URL, "1", [lost]))

    def test_each_status_and_each_way_a_copy_is_named(self):
        """A copy as ``PostsRepo.record_success`` defines one: a confirmed status, with a remote id or a URL."""
        lost = _lost(self.platform)
        for status, url, remote_id in (("published", URL, "1"), ("published", URL, None), ("published", None, "1"),
                                       ("draft", URL, "1"), ("unlisted", URL, "1")):
            with self.subTest(status=status, url=url, remote_id=remote_id):
                post_id = self.recorded(PublishResult(url=url, remote_id=remote_id, status=status))
                ((told, _),) = self.sends(post_id, timeout(), held=1)
                self.assertEqual(self.shows(post_id), (status, url, remote_id, lost))
                self.assertEqual((told.status, told.url, told.remote_id, told.notes), (status, url, remote_id, [lost]))


class FindingEveryNoteStays(Recorded):
    """A row that stays published has no reason to lose any note, so all of them stay."""

    def test_a_platforms_own_note_from_the_recorded_success(self):
        post_id = self.recorded(k531.copy(1, [TRIMMED]))
        lost = _lost(self.platform)
        ((told, error),) = self.sends(post_id, timeout(), held=1)
        self.assertEqual((error, told.notes), (f"{TRIMMED}; {lost}", [TRIMMED, lost]))

    def test_the_edited_note_stays_and_settles_by_equality_after(self):
        """``settle_edited`` reads ``_edited`` by equality from ``notes_of``: it must stay one whole note, and settling it
        must leave the note just added, which only a person clears."""
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=2)
        self.changed(post_id)
        self.sends(post_id, k531.copy(1), held=2)
        edited, lost = _edited(self.platform, URL, "1"), _lost(self.platform)
        ((told, error),) = self.sends(post_id, timeout(), held=1)
        self.assertEqual((error, told.notes), (f"{edited}; {lost}", [edited, lost]))
        self.assertTrue(self.publisher.settle_edited(post_id, self.platform, lambda row: True))
        self.assertEqual(self.shows(post_id), ("published", URL, "1", lost))

    def test_a_note_with_the_separator_in_it_comes_back_as_it_was(self):
        """A platform's own note may hold "; " (``join_notes``): the answer adds to the stored text, it does not reword it."""
        post_id = self.recorded(k531.copy(1, ["Findable said: a; b"]))
        lost = _lost(self.platform)
        ((_, error),) = self.sends(post_id, timeout(), held=1)
        self.assertEqual(error, f"Findable said: a; b; {lost}")


class FindingTheNoteIsWrittenOnceAndOutlivesWhatFollows(Recorded):
    def test_a_second_uncertain_answer_does_not_write_it_twice(self):
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=3)
        self.sends(post_id, k531.copy(1), held=3)
        lost = _lost(self.platform)
        (_, first), (told, second) = self.sends(post_id, timeout(), held=1)[0], self.sends(post_id, RuntimeError("boom"), held=2)[0]
        self.assertEqual((first, second, told.notes), (lost, lost, [lost]))

    def test_a_late_success_of_another_copy_is_refused_and_names_both_once(self):
        post_id = self.recorded()
        lost = _lost(self.platform)
        self.sends(post_id, timeout(), held=1)
        twice = _both_copies(self.platform, _named(URL, "1"), _named("https://findable/9", "9"), 1)
        (_, once), (told, again) = self.sends(post_id, k531.copy(9), k531.copy(9), held=1)
        self.assertEqual((once, again), (f"{lost}; {twice}", f"{lost}; {twice}"))
        self.assertEqual((told.status, told.url), ("published", URL))


class FindingThePostIsNotCountedOrLoggedAsUnconfirmed(Recorded):
    """As a success refused over a copy, which also leaves a note: a warning that says so, and nothing that counts the
    post as a send's result."""

    def test_no_event_no_count_and_the_second_copy_log(self):
        post_id = self.recorded()
        self.measured()
        before = self.events(post_id)
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING") as logged:
            self.sends(post_id, timeout(), held=1)
        self.assertEqual(self.events(post_id), before, "a post that stays published wrote an event")
        self.assertEqual(self.publisher.metrics.snapshot().get("inkcast_publish_total", {}), {}, "it was counted")
        self.assertEqual([getattr(r, "event", None) for r in logged.records], ["publish.second_copy"])
        self.assertIn(_named(URL, "1"), logged.records[0].getMessage())
        self.assertIn(TIMEOUT, logged.records[0].getMessage())

    def test_a_plug_ins_crash_likewise(self):
        post_id = self.recorded()
        self.measured()
        before = self.events(post_id)
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING") as logged:
            self.sends(post_id, RuntimeError("boom"), held=1)
        self.assertEqual(self.events(post_id), before)
        self.assertEqual(self.publisher.metrics.snapshot().get("inkcast_publish_total", {}), {})
        self.assertEqual([getattr(r, "event", None) for r in logged.records if r.levelname == "WARNING"], ["publish.second_copy"])

    def test_a_second_answer_logs_again(self):
        """Each is an answer that may have made a copy, as each refused success logs; the note is one."""
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=3)
        self.sends(post_id, k531.copy(1), held=3)
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING") as logged:
            self.sends(post_id, timeout(), held=1)
            self.sends(post_id, timeout(), held=2)
        self.assertEqual([getattr(r, "event", None) for r in logged.records], ["publish.second_copy"] * 2)


class FindingThePersonIsNotAskedToResendIt(Recorded):
    def test_a_resend_is_refused_and_nothing_more_is_sent(self):
        post_id = self.recorded()
        self.sends(post_id, timeout(), held=1)
        sent = list(self.platform.sent)
        with self.assertRaisesRegex(PlatformError, "only unconfirmed posts can be resent"):
            asyncio.run(self.publisher.resend(self.user, post_id))
        self.assertEqual(self.platform.sent, sent)


class FindingRecordUncertainKnowsNoWording(r214.Harness):
    """The db layer's part, with stand-in wording: the note comes from the caller, as ``keeps`` does for a success."""

    def test_the_note_joins_the_rows_notes_once(self):
        post_id = self.post("publishing")
        self.store.update_post(post_id, status="published", result_url="u/1", attempts=2, error="P; Q")
        self.assertEqual(self.store.record_uncertain(post_id, held=1, error="E", lost="L")[0], False)
        self.assertEqual(self.store.get_post(post_id)["error"], "P; Q; L")
        self.store.record_uncertain(post_id, held=1, error="E", lost="L")
        self.assertEqual(self.store.get_post(post_id)["error"], "P; Q; L")


# ---- what each caller is told -----------------------------------------------------------------------------------------


class Past(pd.SyncHarness):
    """Git sync and the service over Findable, whose send's answer lands after another worker's re-send: A's request is
    out past STUCK_AFTER, B is told "missing" and re-sends, and B's copy is recorded before A's timeout lands."""

    def a_times_out_over_bs_copy(self) -> None:
        b = self.replica()

        async def a_sends(prepared):
            await b.run_due(now=time.time() + STUCK_AFTER + 1)
            raise timeout()

        self.platform.during_publish = a_sends

    def lost(self) -> str:
        return _lost(self.platform)


class FindingGitSyncsLedgerRecordsThePostItIs(Past):
    def test_a_sync_that_made_the_post_records_it_as_published_with_its_copy(self):
        """``GitSync._create``, from the outcome: never an entry reading 'unconfirmed' over a confirmed copy."""
        self.a_times_out_over_bs_copy()
        result = self.sync()
        self.assertEqual((result.action, result.status, result.url, result.message), ("create", "published", URL, self.lost()))
        entry = self.entry()
        self.assertEqual((entry["status"], entry["post_id"], entry["url"], entry["remote_id"], entry["message"]),
                         ("published", result.post_id, URL, "1", None))
        again = self.sync()
        self.assertEqual((again.action, again.status, again.url, self.platform.sent), ("unchanged", "published", URL, ["Hello"]))

    def test_a_sync_following_a_scheduled_post_takes_the_copy(self):
        """``GitSync._one``, from the row: a post the scheduler sent, followed by the next sync. On f5869458 the entry
        read 'unconfirmed', and the sync's check replaced the copy with whatever its lookup named."""
        self.markdown = lambda body="Body.": (f"---\ntitle: Hello\nplatforms: [{self.platform.id}]\n"
                                              f"publish_at: 2020-01-01T00:00:00+00:00\n---\n\n{body}\n")
        scheduled = self.sync()
        self.assertEqual(scheduled.status, "scheduled")
        self.a_times_out_over_bs_copy()
        self.tick()
        followed = self.sync()
        self.assertEqual((followed.action, followed.status, followed.url, self.platform.sent),
                         ("unchanged", "published", URL, ["Hello"]))
        entry = self.entry()
        self.assertEqual((entry["status"], entry["url"], entry["remote_id"]), ("published", URL, "1"))


class FindingTheServiceAndItsClientsAreToldTheNote(Past):
    def published(self):
        self.a_times_out_over_bs_copy()
        request = PublishRequest(markdown=self.markdown())
        return asyncio.run(self.service.publish(self.user, request))

    def test_the_api_reply(self):
        """``POST /api/v1/publish`` returns this response as it is (services/publishing/rest.py)."""
        (result,) = self.published().results
        self.assertEqual((result.status, result.url, result.notes), ("published", URL, [self.lost()]))

    def test_a_resends_reply_and_ledger(self):
        """A person's resend is a send like any other, and its answer can land over a copy too (``InkcastService.resend``)."""
        old = self.unconfirmed()
        self.store.put_entry(self.user, pd.SOURCE, pd.PATH, self.platform.id, post_id=old, status="unconfirmed",
                             content_hash="h")
        self.a_times_out_over_bs_copy()
        reply = asyncio.run(self.service.resend(self.user, old))
        self.assertEqual((reply.status, reply.url, reply.error, reply.first_post_note is not None),
                         ("published", URL, self.lost(), True))
        entry = self.entry()
        self.assertEqual((entry["post_id"], entry["status"], entry["url"], entry["remote_id"], entry["message"]),
                         (reply.id, "published", URL, "1", None))

    def test_the_mcp_reply_shows_the_note(self):
        """``render_publish`` reads a confirmed post's ``notes``, never its ``message``: a note in ``message`` alone
        (``_as_now``) would never reach the person, so the outcome carries it in ``notes``."""
        response = self.published()
        (result,) = response.results
        self.assertEqual(render_publish(response, self.platforms),
                         f"“Hello”\n- findable: published → {URL} (post `{result.post_id}`)\n  note: {self.lost()}")


# ---- what holds: an answer over a row with no confirmed copy, as before -----------------------------------------------


class HeldAnAnswerOverNoCopyIsWrittenAsBefore(Recorded):
    def answered(self, post_id: str, answer: Exception, held: int) -> tuple:
        """(the outcome, its error after, the events it wrote, the count it added, the log lines' events)."""
        self.measured()
        before = len(self.events(post_id))
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING") as logged:
            ((told, error),) = self.sends(post_id, answer, held=held)
        return (told, error, self.events(post_id)[before:], self.counted(),
                [getattr(r, "event", None) for r in logged.records if r.levelname == "WARNING"])

    def test_its_own_answer(self):
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=1)
        told, error, events, counted, logs = self.answered(post_id, timeout(), held=1)
        self.assertEqual((told.status, told.url, told.remote_id, told.message, told.notes),
                         ("unconfirmed", None, None, TIMEOUT, []))
        self.assertEqual((self.store.get_post(post_id)["status"], error), ("unconfirmed", TIMEOUT))
        self.assertEqual((events, counted, logs), (["post.unconfirmed"], 1, ["publish.unconfirmed"]))

    def test_over_a_later_attempt_it_notes_that_it_may_have_arrived_too(self):
        """#234: over every status with no copy, the row's error is replaced with the answer and ``_lost``."""
        lost = _lost(self.platform)
        for status in ("publishing", "recovering", "scheduled", "failed", "unconfirmed"):
            with self.subTest(status=status):
                post_id = self.post("publishing")
                self.store.update_post(post_id, status=status, attempts=2, error="What the row said before.")
                told, error, events, counted, logs = self.answered(post_id, timeout(), held=1)
                self.assertEqual((told.status, told.url, told.remote_id, told.message, told.notes),
                                 ("unconfirmed", None, None, TIMEOUT, []))
                self.assertEqual((self.store.get_post(post_id)["status"], error), ("unconfirmed", f"{TIMEOUT}; {lost}"))
                self.assertEqual((events, counted, logs), (["post.unconfirmed"], 1, ["publish.unconfirmed"]))

    def test_a_confirmed_status_with_neither_a_url_nor_an_id_records_no_copy(self):
        """A row from before #214 could read 'published' with nothing to find it by: nothing there to keep."""
        lost = _lost(self.platform)
        post_id = self.post("publishing")
        self.store.update_post(post_id, status="published", attempts=2)
        told, error, events, counted, logs = self.answered(post_id, timeout(), held=1)
        self.assertEqual((told.status, self.store.get_post(post_id)["status"], error),
                         ("unconfirmed", "unconfirmed", f"{TIMEOUT}; {lost}"))
        self.assertEqual((events, counted, logs), (["post.unconfirmed"], 1, ["publish.unconfirmed"]))

    def test_a_plug_ins_crash_is_written_likewise_and_logs_as_before(self):
        lost = _lost(self.platform)
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=2)
        told, error, events, counted, logs = self.answered(post_id, RuntimeError("boom"), held=1)
        self.assertEqual((told.status, told.message), ("unconfirmed", "Internal error in the Findable plug-in; the post may "
                                                       "or may not have been published. Check Findable before trying again."))
        self.assertEqual(error, f"internal error: boom; {lost}")
        self.assertEqual((events, counted, logs), (["post.unconfirmed"], 1, []))


class HeldALateSuccessOfTheCopyRecordedKeepsTheNote(Recorded):
    """On f5869458 the same success landed over 'unconfirmed', and kept the note as it keeps ``_lost`` (#234)."""

    def test_the_note_stays(self):
        post_id = self.recorded()
        lost = _lost(self.platform)
        self.sends(post_id, timeout(), held=1)
        ((told, error),) = self.sends(post_id, k531.copy(1), held=2)
        self.assertEqual((told.status, error, told.notes), ("published", lost, [lost]))


class HeldTheFencesRefuseAndSayNothing(Recorded):
    """Nothing written, no note, no event, no count, no log line, and the caller told what the post is."""

    def refused(self, post_id: str, answer: Exception, held: int):
        self.measured()
        before, row = len(self.events(post_id)), dict(self.store.get_post(post_id))
        with self.assertNoLogs("inkcast.libs.pipeline", level="WARNING"):
            ((told, _),) = self.sends(post_id, answer, held=held)
        self.assertEqual(dict(self.store.get_post(post_id)), row, "the refused answer changed the row")
        self.assertEqual((self.events(post_id)[before:], self.counted()), ([], 0))
        return told

    def test_a_copy_found_at_this_very_attempt_takes_no_note(self):
        """Another worker's lookup found the very send this answer is about, with no send since (#226)."""
        post_id = self.recorded(k531.copy(1, [TRIMMED]))
        told = self.refused(post_id, timeout(), held=2)
        self.assertEqual((told.status, told.url, told.remote_id, told.message, told.notes),
                         ("published", URL, "1", TRIMMED, []))

    def test_a_cancel_stays(self):
        """#216, #226: at this attempt, and at a later one."""
        for attempts, held in ((1, 1), (2, 1)):
            with self.subTest(attempts=attempts, held=held):
                post_id = self.post("cancelled")
                self.store.update_post(post_id, attempts=attempts, error="Cancelled by the person.")
                told = self.refused(post_id, timeout(), held=held)
                self.assertEqual((told.status, told.url, told.message), ("cancelled", None, "Cancelled by the person."))

    def test_a_crash_falls_to_the_same_fences(self):
        post_id = self.recorded()
        with self.assertLogs("inkcast.libs.pipeline", level="ERROR"):      # the crash's own traceback, no more
            ((told, error),) = self.sends(post_id, RuntimeError("boom"), held=2)
        self.assertEqual((told.status, told.url, error), ("published", URL, None))

    def test_a_post_erased_meanwhile_keeps_the_sends_answer(self):
        post_id = self.post("publishing")
        prepared = self.platform.prepare({"title": "t"}, "Body.")

        async def erased(_prepared):
            self.store._exec("DELETE FROM posts WHERE id = ?", post_id)
            raise timeout()

        self.platform.during_publish = erased
        told = asyncio.run(self.publisher._send(post_id, self.user, self.platform, prepared, attempts=1, held=0))
        self.assertEqual((told.status, told.message), ("unconfirmed", TIMEOUT))


class HeldTheReadAndTheWriteShareTheWriteLock(Recorded):
    """At every statement of the write, another connection is asked for the write lock it would need to land a claim or
    a success. Nothing that reads the post before the write runs without the lock held."""

    def watched(self, post_id: str, answer: Exception, held: int) -> list[tuple[str, bool]]:
        other = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        self.addCleanup(other.close)
        seen: list[tuple[str, bool]] = []

        def statement(sql: str) -> None:
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
                seen.append((sql, True))
            except sqlite3.OperationalError:
                seen.append((sql, False))

        self.store._conn.set_trace_callback(statement)
        self.addCleanup(self.store._conn.set_trace_callback, None)
        prepared = self.platform.prepare({"title": "t"}, "Body.")
        self.platform.answer = answer
        asyncio.run(self.publisher._send(post_id, self.user, self.platform, prepared, attempts=held or 1, held=held))
        self.store._conn.set_trace_callback(None)
        return seen

    def assert_locked(self, seen: list[tuple[str, bool]]) -> None:
        write = next(i for i, (sql, _) in enumerate(seen) if sql.startswith("UPDATE posts"))
        begin = max(i for i, (sql, _) in enumerate(seen[:write]) if sql == "BEGIN IMMEDIATE")
        commit = next(i for i, (sql, _) in enumerate(seen) if i > write and sql == "COMMIT")
        self.assertEqual([sql for sql, free in seen[begin + 1:commit + 1] if free], [], "a statement ran with the lock free")
        self.assertEqual([sql for sql, _ in seen[:begin] if " posts " in sql], [], "the post was read outside the transaction")

    def test_over_a_copy_recorded(self):
        self.assert_locked(self.watched(self.recorded(), timeout(), held=1))

    def test_over_no_copy(self):
        post_id = self.post("publishing")
        self.store.update_post(post_id, attempts=2)
        self.assert_locked(self.watched(post_id, timeout(), held=1))


if __name__ == "__main__":
    unittest.main()
