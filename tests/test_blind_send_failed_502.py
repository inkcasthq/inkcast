# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #502: a send that never began, on a platform Inkcast can't ask whether a post arrived (no ``find``, as Medium),
was called a restart, and no alert stayed on.

A scheduler on the wrong INKCAST_SECRET_KEY can't decrypt a due post's credentials: the send can't begin, it is counted
``send_failed`` (#472) and the post is left claimed. At the next stuck pass, recovery can't ask the platform, so it
marked the post 'unconfirmed' with "Inkcast restarted while this post was being sent", although nothing was sent, and
counted it ``recovered``: InkcastUnconfirmedPublishes never saw it, and InkcastPostsCannotBeSent resolved 30 minutes
after the last due post while the post waited for its owner. Now the claim records that the send never began, the
owner is told what happened (reconnect, then resend or cancel), and the post is counted ``unconfirmed``.

Each ``Finding`` test failed on main (e76004e3). The ``Held`` tests cover paths that must stay safe."""

import asyncio
import time
import unittest

from inkcast.libs.alerting import ALERTS
from inkcast.libs.config import Config
from inkcast.libs.observability import EVENTS
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, PlatformError
from inkcast.libs.runtime import build_metrics
from inkcast.libs.secrets import CredentialsUnreadable

from test_person_decides import Blind, Findable
from test_wrong_key_472 import RUNBOOK, WrongKeyScheduler

RESTARTED = "Inkcast restarted while this post was being sent"
UNSENT_MARKER = ("Stored credentials could not be decrypted when this post was due (they were sealed "
                 "under another INKCAST_SECRET_KEY than the scheduler runs on), so it was not sent.")


class OnABlindPlatform(WrongKeyScheduler):
    platform_class = Blind

    def never_sent(self) -> str:
        """A due post the wrong-key scheduler could not begin to send, then its stuck pass."""
        post_id = self.post("scheduled")
        self.tick(publisher=self.wrong)
        self.assertEqual((self.counted("send_failed"), self.status(post_id)), (1, "publishing"))
        with self.assertLogs("inkcast.libs.pipeline", level="WARNING") as self.logged:
            self.tick(later=STUCK_AFTER + 1, publisher=self.wrong)
        return post_id


class FindingASendThatNeverBeganIsNotCalledARestart(OnABlindPlatform):
    def test_the_owner_is_told_the_credentials_could_not_be_decrypted_and_to_reconnect(self):
        """The post was unconfirmed with "Inkcast restarted while this post was being sent to Blind, which can't be
        checked automatically", though the send never began."""
        post_id = self.never_sent()
        post = self.store.get_post(post_id)
        self.assertEqual(post["status"], "unconfirmed")
        self.assertNotIn(RESTARTED, post["error"])                            # the restart's words on main
        self.assertEqual(post["error"], (
            "Stored credentials could not be decrypted when this post was due (they were sealed under another "
            "INKCAST_SECRET_KEY than the scheduler runs on), so it was not sent. Reconnect Blind (inkcast_connect, or "
            f"POST /api/v1/platforms/blind/connect-link), then resend it (inkcast_resend, or POST /api/v1/posts/{post_id}"
            f"/resend) or cancel it (inkcast_cancel, or POST /api/v1/posts/{post_id}/cancel)."))
        self.assertEqual(self.platform.sent, [])

    def test_a_display_name_change_does_not_turn_the_unsent_post_into_a_restart(self):
        post_id = self.post("scheduled")
        self.tick(publisher=self.wrong)
        self.assertEqual(self.status(post_id), "publishing")
        self.platform.name = "Renamed Blind"
        self.tick(later=STUCK_AFTER + 1, publisher=self.wrong)
        post = self.store.get_post(post_id)
        self.assertEqual(post["status"], "unconfirmed")
        self.assertTrue(post["error"].startswith(UNSENT_MARKER))
        self.assertIn("not sent", post["error"])
        self.assertNotIn(RESTARTED, post["error"])
        self.assertEqual((self.platform.sent, self.counted("unconfirmed")), ([], 1))


class HeldTheOwnersResendIsTheOneSend(OnABlindPlatform):
    def test_it_was_not_sent_so_the_owners_resend_sends_it_once(self):
        """The owner's resend, on a process that reads the credentials, is the one send: nothing went out before it."""
        post_id = self.never_sent()
        outcome = asyncio.run(self.service.resend(self.user, post_id))
        self.assertEqual((outcome.status, self.status(post_id), self.platform.sent), ("published", "cancelled", ["t"]))


class FindingAnUnsentPostKeepsItsHistoryAfterAnOwnerDecision(OnABlindPlatform):
    def test_cancel_does_not_claim_the_unsent_post_may_be_on_the_platform(self):
        post_id = self.never_sent()
        cancelled, was = self.service.cancel_from(self.user, post_id)
        self.assertEqual(was, "unconfirmed")
        self.assertIn("not sent", cancelled.error)
        self.assertNotIn("may already be", cancelled.error)
        self.assertEqual(self.platform.sent, [])

    def test_resend_does_not_claim_the_old_unsent_post_may_be_on_the_platform(self):
        post_id = self.never_sent()
        asyncio.run(self.service.resend(self.user, post_id))
        old = self.store.get_post(post_id)
        self.assertEqual(old["status"], "cancelled")
        self.assertIn("not sent", old["error"])
        self.assertNotIn("may already be", old["error"])
        self.assertEqual(self.platform.sent, ["t"])

    def test_a_cancel_before_recovery_writes_is_not_overwritten_or_counted(self):
        post_id = self.post("scheduled")
        self.tick(publisher=self.wrong)
        update = self.wrong.store.update_post

        def cancel_before_write(candidate, *args, **fields):
            if candidate == post_id and fields.get("status") == "unconfirmed":
                self.wrong.store.update_post = update
                self.service.cancel_from(self.user, post_id)
            return update(candidate, *args, **fields)

        self.wrong.store.update_post = cancel_before_write
        self.tick(later=STUCK_AFTER + 1, publisher=self.wrong)
        post = self.store.get_post(post_id)
        self.assertEqual(post["status"], "cancelled")
        self.assertIn("not sent", post["error"])
        self.assertEqual(self.counted("unconfirmed"), 0)


class FindingSuchAPostIsCountedWhereTheUnconfirmedAlertSeesIt(OnABlindPlatform):
    def test_it_is_counted_unconfirmed_not_recovered_and_logged_by_id(self):
        """Counted ``recovered``: InkcastUnconfirmedPublishes, which reads only ``unconfirmed``, never fired, and
        InkcastPostsCannotBeSent resolved 30 minutes after the send while the post waited for its owner."""
        post_id = self.never_sent()
        self.assertEqual((self.counted("unconfirmed"), self.counted("recovered")), (1, 0))   # (0, 1) on main
        rule = next(a for a in ALERTS if a.name == "InkcastUnconfirmedPublishes")
        self.assertIn('inkcast_publish_total{status="unconfirmed"}', rule.expr)
        unsent = [r for r in self.logged.records if getattr(r, "event", None) == "publish.recovery.unsent"]
        self.assertEqual([(r.post_id, r.platform) for r in unsent], [(post_id, "blind")])
        self.assertIn("publish.recovery.unsent", EVENTS)


class FindingTheUnconfirmedCountStartsAtZero(unittest.TestCase):
    def test_every_platform_exposes_the_unconfirmed_count_at_zero_from_the_start(self):
        """The series was made by its first increment, and ``increase()`` sees no rise in a series' first sample: the
        first post a pod counted unconfirmed on a platform never fired InkcastUnconfirmedPublishes (promtool 3.7.0: a
        series first scraped at 1 gave no sample; first scraped at 0, then 1, gave 1), the alert this count is for."""
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:")
        rendered = build_metrics(None, {"devto": None, "medium": None}, cfg).render()
        for platform in ("devto", "medium"):
            with self.subTest(platform=platform):
                self.assertIn(f'inkcast_publish_total{{platform="{platform}",status="unconfirmed"}} 0', rendered)


class HeldASendThatBeganIsStillARestart(OnABlindPlatform):
    def interrupted(self, **fields) -> str:
        """A post whose send began, and the process stopped: 'publishing', untouched for longer than STUCK_AFTER."""
        post_id = self.post("publishing", **fields)
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        return post_id

    def test_a_restart_mid_send_on_a_scheduler_that_cannot_read_the_credentials_is_still_a_restart(self):
        """The request may have reached the platform: the restart's words and its count, whatever the key of the
        scheduler that makes the stuck pass, and whatever an earlier attempt left in the error."""
        for fields in ({}, {"error": "retrying: Blind rate limit hit; try again in 15 minutes."}):
            with self.subTest(fields=fields):
                before = self.counted("recovered")
                post_id = self.interrupted(**fields)
                self.tick(publisher=self.wrong)
                post = self.store.get_post(post_id)
                self.assertEqual(post["status"], "unconfirmed")
                self.assertIn(f"{RESTARTED} to Blind, which can't be checked automatically.", post["error"])
                self.assertEqual((self.counted("recovered") - before, self.counted("unconfirmed")), (1, 0))

    def test_a_claim_taken_over_before_the_read_failed_keeps_what_it_became(self):
        """The record that the send never began is written only while the claim is still this send's: another worker
        that took the post over and resolved it meanwhile keeps its answer."""
        post_id = self.post("scheduled")
        resolved = f"{RESTARTED} to Blind, which can't be checked automatically."
        read = self.wrong.store.get_credentials

        def taken_over_then_unreadable(user_id, platform_id):
            self.store.update_post(post_id, status="unconfirmed", error=resolved)
            return read(user_id, platform_id)

        self.wrong.store.get_credentials = taken_over_then_unreadable
        self.tick(publisher=self.wrong)
        self.assertEqual((self.status(post_id), self.store.get_post(post_id)["error"]), ("unconfirmed", resolved))

    def test_a_later_publishing_claim_does_not_receive_the_earlier_claims_marker(self):
        post_id = self.post("scheduled")
        read = self.wrong.store.get_credentials

        def reclaimed_then_unreadable(user_id, platform_id):
            self.store.update_post(post_id, status="scheduled", error="later claim", run_at=time.time() - 1)
            later = self.store.claim_due_posts(time.time())
            self.assertEqual([r["id"] for r in later], [post_id])
            return read(user_id, platform_id)

        self.wrong.store.get_credentials = reclaimed_then_unreadable
        self.tick(publisher=self.wrong)
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["attempts"], post["error"]),
                         ("publishing", 2, "later claim"))


class HeldAPayloadThisVersionCannotReadIsNotMarked(OnABlindPlatform):
    def test_it_stays_recovering_and_every_stuck_pass_counts_it(self):
        """A reconnect does not mend a payload a newer version wrote (a rollback): only the credentials' error records a
        send that never began. Such a post's recovery can't be made either, so it is counted at every stuck pass
        (``recovery_failed``), and InkcastPostsCannotBeSent stays on while it waits, as before."""
        post_id = self.post("scheduled")
        self.store._exec("UPDATE posts SET payload = ? WHERE id = ?", '{"platform": "blind"}', post_id)
        right = Publisher(self.store, self.platforms, metrics=self.wrong.metrics)
        self.tick(publisher=right)
        for n in (1, 2):
            self.tick(later=n * (STUCK_AFTER + 1), publisher=right)
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["error"]), ("recovering", None))
        self.assertEqual((self.counted("send_failed"), self.counted("recovery_failed"), self.counted("unconfirmed")),
                         (1, 2, 0))

    def test_a_recovery_failure_does_not_prove_that_an_earlier_send_never_arrived(self):
        post_id = self.post("publishing")
        prepared = self.platform.prepare({"title": "t"}, "Body.")
        asyncio.run(self.platform.publish({}, Account(id="1", name="blind"), prepared, None))
        self.store._exec("UPDATE posts SET payload = ?, updated_at = updated_at - ? WHERE id = ?",
                         '{"platform": "blind"}', STUCK_AFTER + 1, post_id)
        self.tick()
        self.assertEqual((self.status(post_id), self.platform.sent), ("recovering", ["t"]))
        section = RUNBOOK.read_text().split("## InkcastPostsCannotBeSent", 1)[1].split("\n## ", 1)[0]
        rule = next(a for a in ALERTS if a.name == "InkcastPostsCannotBeSent")
        self.assertNotIn("None of these posts is sent", section)
        self.assertNotIn("None of these posts is sent", rule.description)
        self.assertIn("A recovery failure cannot establish whether an earlier send arrived", section)
        self.assertIn("A recovery failure cannot establish whether an earlier send arrived", rule.description)


class HeldACredentialErrorInsideThePlugInIsAmbiguous(OnABlindPlatform):
    def test_an_error_after_publish_begins_does_not_receive_the_unsent_marker(self):
        post_id = self.post("scheduled")

        async def failed_inside_publish(_prepared):
            raise CredentialsUnreadable("Synthetic error after the plug-in call began")

        self.platform.during_publish = failed_inside_publish
        self.tick()
        post = self.store.get_post(post_id)
        self.assertEqual(post["status"], "unconfirmed")
        self.assertTrue(post["error"].startswith("internal error:"))
        self.assertEqual(self.platform.sent, [])


class HeldAPlugInErrorCannotForgeTheUnsentHistory(OnABlindPlatform):
    def ambiguous(self) -> str:
        post_id = self.post("scheduled")

        async def sent_then_lost(_prepared):
            self.platform.sent.append("t")
            raise PlatformError(UNSENT_MARKER, uncertain=True)

        self.platform.during_publish = sent_then_lost
        self.tick()
        self.assertEqual(self.status(post_id), "unconfirmed")
        return post_id

    def test_cancel_still_warns_that_the_post_may_be_on_the_platform(self):
        post_id = self.ambiguous()
        cancelled, _ = self.service.cancel_from(self.user, post_id)
        self.assertIn("may already be", cancelled.error)
        self.assertEqual(self.platform.sent, ["t"])

    def test_resend_still_warns_that_the_old_post_may_be_on_the_platform(self):
        post_id = self.ambiguous()
        asyncio.run(self.service.resend(self.user, post_id))
        old = self.store.get_post(post_id)
        self.assertIn("may already be", old["error"])
        self.assertEqual(self.platform.sent, ["t", "t"])


class HeldAMissingPlugInDoesNotUseTheMarker(OnABlindPlatform):
    def test_a_due_post_with_no_registered_plug_in_fails_before_the_send_block(self):
        post_id = self.post("scheduled")
        self.platforms.clear()
        self.tick(publisher=self.wrong)
        post = self.store.get_post(post_id)
        self.assertEqual(post["status"], "failed")
        self.assertIn("not installed", post["error"])
        self.assertEqual((self.counted("send_failed"), self.platform.sent), (0, []))


class HeldAPlatformThatCanBeAskedStillAsks(WrongKeyScheduler):
    platform_class = Findable

    def test_the_post_stays_recovering_and_a_scheduler_on_the_right_key_sends_it(self):
        """On a platform Inkcast can ask, a send that never began is asked about as before (#472): 'recovering' while
        the lookup can't be made, and sent once a scheduler that reads the credentials finds it missing."""
        post_id = self.post("scheduled")
        self.tick(publisher=self.wrong)
        self.tick(later=STUCK_AFTER + 1, publisher=self.wrong)
        self.assertEqual((self.status(post_id), self.counted("recovery_failed"), self.counted("unconfirmed")),
                         ("recovering", 1, 0))
        right = Publisher(self.store, self.platforms)
        self.tick(later=2 * (STUCK_AFTER + 1), publisher=right)              # missing: queued again
        self.tick(later=2 * (STUCK_AFTER + 1), publisher=right)              # and sent
        self.assertEqual((self.status(post_id), self.platform.sent), ("published", ["t"]))

    def test_its_truthful_error_is_visible_while_recovering(self):
        post_id = self.post("scheduled")
        self.tick(publisher=self.wrong)
        self.tick(later=STUCK_AFTER + 1, publisher=self.wrong)
        listed = next(p for p in self.service.list_posts(self.user) if p.id == post_id)
        self.assertEqual(listed.status, "recovering")
        self.assertIn("not sent", listed.error)


class FindingTheRunbookSaysWhatTheOwnerSees(unittest.TestCase):
    def test_step_4_gives_the_owners_words_not_the_restarts(self):
        """Step 4 said the post is marked unconfirmed with "Inkcast restarted while this post was being sent"."""
        text = RUNBOOK.read_text()
        section = text.split("## InkcastPostsCannotBeSent", 1)[1].split("\n## ", 1)[0]
        step4 = section.split("\n4. ", 1)[1]
        self.assertNotIn(RESTARTED, step4)
        for named in ("could not be decrypted when this post was due", "so it was not sent", "Reconnect",
                      "InkcastUnconfirmedPublishes"):
            self.assertIn(named, step4)
        unconfirmed = text.split("## InkcastUnconfirmedPublishes", 1)[1].split("\n## ", 1)[0]
        self.assertIn("publish.recovery.unsent", unconfirmed)

    def test_step_4_does_not_promise_a_resend_when_the_stored_key_still_differs(self):
        text = RUNBOOK.read_text()
        step4 = text.split("## InkcastPostsCannotBeSent", 1)[1].split("\n4. ", 1)[1].split("\n## ", 1)[0]
        self.assertIn("If the corrected key decrypts the stored credentials", step4)


if __name__ == "__main__":
    unittest.main()
