# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Codex's review of PR #499 (issue #472), the findings the integrator took: a resend sends with the credentials it
verified, the publish failure rate counts attempts only, and a wrong-key send's log line holds no secret."""

import asyncio
import unittest

from inkcast.libs.alerting import ALERTS
from inkcast.libs.db import Store
from inkcast.libs.observability import JsonFormatter
from test_person_decides import Harness


class FindingAResendSendsWithTheCredentialsItVerified(Harness):
    def test_a_reseal_under_another_key_after_the_cancel_does_not_fail_the_send(self):
        """``_send`` read the credentials again after ``resend_post`` had cancelled the old post: another process
        sealing them under another key in between failed the send with ``CredentialsUnreadable``, a 500 after the
        cancel (measured on dd95bed5)."""
        post_id = self.unconfirmed()
        other = Store(self.path, "j" * 40)
        self.addCleanup(other.close)
        original = self.store.resend_post

        def reseal_after_cancel(*args, **kwargs):
            done = original(*args, **kwargs)
            other.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
            return done

        self.store.resend_post = reseal_after_cancel
        outcome = asyncio.run(self.publisher.resend(self.user, post_id))
        self.assertEqual(outcome.status, "published")
        self.assertEqual(self.platform.sent, ["t"])
        self.assertEqual(self.status(post_id), "cancelled")


class FindingTheFailureRateCountsAttemptsOnly(unittest.TestCase):
    def test_a_send_that_could_not_begin_is_left_out_of_the_ratio_and_the_volume(self):
        """A ``send_failed`` count is no attempt at the platform: in the denominator and the volume gate it would dilute
        InkcastPublishFailureRateHigh, which before #472 counted every status there was."""
        rule = next(alert for alert in ALERTS if alert.name == "InkcastPublishFailureRateHigh")
        self.assertEqual(rule.expr.count('inkcast_publish_total{status!="send_failed"}[30m]'), 2, rule.expr)


class HeldAWrongKeySendLogsNoSecret(Harness):
    def test_the_log_line_names_the_post_and_the_platform_only(self):
        post_id = self.post("scheduled")
        other_key, credential = "j" * 40, "CREDENTIAL_VALUE_NEVER_LOG_472"
        other = Store(self.path, other_key)
        self.addCleanup(other.close)
        other.save_credentials(self.user, self.platform.id, {"token": credential}, {"id": "1", "name": "findable"})
        with self.assertLogs("inkcast.libs.pipeline", level="ERROR") as captured:
            self.tick()
        failed = [r for r in captured.records if getattr(r, "event", None) == "publish.send.failed"]
        self.assertEqual(len(failed), 1)
        line = JsonFormatter().format(failed[0])
        for absent in (other_key, credential, "Body."):
            self.assertNotIn(absent, line)
        self.assertEqual((failed[0].post_id, failed[0].platform), (post_id, "findable"))


if __name__ == "__main__":
    unittest.main()
