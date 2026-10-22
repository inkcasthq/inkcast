# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""F1.10.S1 (issue #89): the publish retry policy is settings.

``INKCAST_PUBLISH_MAX_ATTEMPTS`` (4; 1-10) and ``INKCAST_PUBLISH_RETRY_DELAYS`` (60,300,1800; 1-9 whole seconds, each
30-21600, never decreasing; the last one repeats) reach the Publisher as a ``RetryPolicy`` passed in, not read from a
module global. The defaults are exactly what ``libs/pipeline.py`` hard-coded before. An uncertain error is never retried
whatever the policy, and a retry window over 24 hours is a posture warning."""

import asyncio
import dataclasses
import time
import unittest

import httpx2

from inkcast.libs import pipeline
from inkcast.libs.config import Config, ConfigError, RetryPolicy, load_config
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.libs.secrets import Redactor, run_checks

BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
        "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
ATTEMPTS, DELAYS = "INKCAST_PUBLISH_MAX_ATTEMPTS", "INKCAST_PUBLISH_RETRY_DELAYS"
# libs/pipeline.py before this story: MAX_ATTEMPTS = 4, RETRY_DELAYS = [60, 300, 1800], the last one repeated.
TODAY = RetryPolicy(max_attempts=4, delays=(60, 300, 1800))
DAY = 24 * 3600


def config(**env) -> Config:
    return load_config({**BASE, **env})


def by_hand(**fields) -> Config:
    return Config(public_url="https://inkcast.example.com", secret_key=BASE["INKCAST_SECRET_KEY"], database_path=":memory:",
                  login="password", password="p" * 12, **fields)


class MaxAttempts(unittest.TestCase):
    def test_from_1_to_10_and_the_default_is_4(self):
        self.assertEqual(config().publish_retry.max_attempts, 4)
        self.assertEqual(config(INKCAST_PUBLISH_MAX_ATTEMPTS="").publish_retry.max_attempts, 4)
        for raw, value in (("1", 1), ("10", 10), (" 7 ", 7)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_PUBLISH_MAX_ATTEMPTS=raw)
                self.assertEqual(cfg.publish_retry.max_attempts, value)
                cfg.validate()

    def test_outside_the_range_or_not_a_whole_number_is_a_configuration_error_naming_the_variable(self):
        for raw in ("0", "11", "-1", "100", "4.5", "four", "1e1", "0x4", "4,5", "inf", "true", "∞"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{ATTEMPTS} must be a whole number from 1 to 10") as caught:
                    config(INKCAST_PUBLISH_MAX_ATTEMPTS=raw)
                self.assertIn(repr(raw), str(caught.exception), "the error quotes the value it refused")
                self.assertNotIn(DELAYS, str(caught.exception), "only the variable at fault is named")


class RetryDelays(unittest.TestCase):
    def test_1_to_9_values_each_30_to_21600_seconds_and_the_default(self):
        self.assertEqual(config().publish_retry.delays, (60, 300, 1800))
        self.assertEqual(config(INKCAST_PUBLISH_RETRY_DELAYS="").publish_retry.delays, (60, 300, 1800))
        for raw, delays in (("30", (30,)), ("21600", (21600,)), ("30,21600", (30, 21600)), (" 60, 300 ,1800 ", (60, 300, 1800)),
                            (",".join(["30"] * 9), (30,) * 9), (",".join(["21600"] * 9), (21600,) * 9)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_PUBLISH_RETRY_DELAYS=raw)
                self.assertEqual(cfg.publish_retry.delays, delays)
                cfg.validate()

    def test_never_decreasing_means_equal_is_fine_and_a_drop_anywhere_is_not(self):
        self.assertEqual(config(INKCAST_PUBLISH_RETRY_DELAYS="60,60,60").publish_retry.delays, (60, 60, 60))
        self.assertEqual(config(INKCAST_PUBLISH_RETRY_DELAYS="30,30,300,300").publish_retry.delays, (30, 30, 300, 300))
        for raw in ("300,60", "60,1800,300", "60,300,299", "30,30,31,30", "21600,30"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{DELAYS} must be 1 to 9 whole numbers of seconds.*never decreasing"):
                    config(INKCAST_PUBLISH_RETRY_DELAYS=raw)

    def test_each_bound_and_shape_is_a_configuration_error_naming_the_variable(self):
        too_many = ",".join(["30"] * 10)
        for raw in ("29", "0", "-60", "21601", "86400", "60,29", "60,21601", too_many, "60,,300", "60;300", "60 300", "60.5",
                    "a,b", "60,300,1800,", ",60", "1e2", "0x3c", "thirty", ","):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{DELAYS} must be 1 to 9 whole numbers of seconds") as caught:
                    config(INKCAST_PUBLISH_RETRY_DELAYS=raw)
                self.assertIn(repr(raw), str(caught.exception), "the error quotes the value it refused")
                self.assertNotIn(ATTEMPTS, str(caught.exception), "only the variable at fault is named")

    def test_both_wrong_names_both(self):
        with self.assertRaises(ConfigError) as caught:
            config(INKCAST_PUBLISH_MAX_ATTEMPTS="0", INKCAST_PUBLISH_RETRY_DELAYS="0")
        self.assertIn(ATTEMPTS, str(caught.exception))
        self.assertIn(DELAYS, str(caught.exception))


class ConfigBuiltByHand(unittest.TestCase):
    def test_validate_applies_the_same_rules(self):
        for policy in (TODAY, RetryPolicy(1, (30,)), RetryPolicy(10, (21600,) * 9), RetryPolicy(10, (30, 30, 21600))):
            with self.subTest(policy=policy):
                dataclasses.replace(by_hand(), publish_retry=policy).validate()
        for policy, name in (
            (RetryPolicy(0, (60,)), ATTEMPTS), (RetryPolicy(11, (60,)), ATTEMPTS), (RetryPolicy(4.0, (60,)), ATTEMPTS),
            (RetryPolicy("4", (60,)), ATTEMPTS), (RetryPolicy(True, (60,)), ATTEMPTS),
            (RetryPolicy(4, ()), DELAYS), (RetryPolicy(4, (29,)), DELAYS), (RetryPolicy(4, (21601,)), DELAYS),
            (RetryPolicy(4, (300, 60)), DELAYS), (RetryPolicy(4, (60.0,)), DELAYS), (RetryPolicy(4, (30,) * 10), DELAYS),
            (RetryPolicy(4, "60"), DELAYS),
        ):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ConfigError, name):
                    dataclasses.replace(by_hand(), publish_retry=policy).validate()


class Flaky(Platform):
    """Fails every publish the way it is told to, and counts the sends."""

    id, name, default_api_url, credential_fields = "flaky", "Flaky", "https://flaky.invalid", []

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sends = 0
        self.error: BaseException | None = PlatformError("429: slow down", retryable=True)

    async def verify(self, creds):
        return Account(id="1", name="flaky")

    def prepare(self, fields, body):
        return Prepared(platform="flaky", title=fields["title"], payload={}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        if self.error is not None:
            raise self.error
        return PublishResult(url="https://flaky/1", remote_id="1", status="published")


class Scheduler(unittest.TestCase):
    """A scheduled post on a platform that keeps failing, ticked until the pipeline stops rescheduling it."""

    def publisher(self, policy: RetryPolicy | None = None) -> Publisher:
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:usr", "usr", None, None)["id"]
        self.platform = Flaky()
        self.store.save_credentials(self.user, "flaky", {"k": "v"}, {"id": "1", "name": "flaky"})
        if policy is None:
            return Publisher(self.store, {"flaky": self.platform})          # the default policy
        return Publisher(self.store, {"flaky": self.platform}, retry=policy)

    def due(self) -> str:
        return self.store.create_post(group_id="g", user_id=self.user, platform="flaky", title="t", status="scheduled",
                                      run_at=time.time() - 1, payload={"platform": "flaky", "title": "t", "payload": {},
                                                                       "preview": {}})

    def drive(self, publisher: Publisher, post_id: str) -> tuple[int, list[int], str]:
        """Tick whenever the post is due: (sends, the delay before each retry, the final status)."""
        delays: list[int] = []
        for _ in range(40):
            post = self.store.get_post(post_id)
            if post["status"] != "scheduled":
                break
            asyncio.run(publisher.run_due(now=post["run_at"] + 1))
            post = self.store.get_post(post_id)
            if post["status"] == "scheduled":                    # rescheduled: run_at was set to now + delay
                delays.append(round(post["run_at"] - post["updated_at"]))
        return self.platform.sends, delays, self.store.get_post(post_id)["status"]


class DefaultPolicy(Scheduler):
    def test_the_default_is_what_the_pipeline_hard_coded(self):
        self.assertEqual(RetryPolicy(), TODAY)
        self.assertEqual(config().publish_retry, TODAY)
        self.assertEqual(TODAY.window_seconds, 60 + 300 + 1800, "about 36 minutes")
        self.assertEqual([TODAY.delay_after(a) for a in (1, 2, 3, 4, 9)], [60, 300, 1800, 1800, 1800])

    def test_a_publisher_given_no_policy_retries_as_before(self):
        publisher = self.publisher()
        self.assertEqual(self.drive(publisher, self.due()), (4, [60, 300, 1800], "failed"))

    def test_the_policy_is_passed_in_not_a_module_global(self):
        for name in ("MAX_ATTEMPTS", "RETRY_DELAYS"):
            self.assertFalse(hasattr(pipeline, name), f"libs/pipeline.{name} is global state; the policy is passed in")
        from inkcast.libs.runtime import build_context
        ctx = build_context(by_hand(publish_retry=RetryPolicy(7, (30, 90))))
        self.addCleanup(ctx.store.close)
        self.assertEqual(ctx.publisher.retry, RetryPolicy(7, (30, 90)))


class CustomPolicy(Scheduler):
    def test_the_attempt_count_and_the_delays_are_the_policy_s(self):
        for policy, sends, delays in (
            (RetryPolicy(3, (45, 90)), 3, [45, 90]),
            (RetryPolicy(1, (30,)), 1, []),                                   # one attempt: no retry at all
            (RetryPolicy(6, (30,)), 6, [30] * 5),                             # one delay: it repeats
            (RetryPolicy(5, (30, 60, 120, 240)), 5, [30, 60, 120, 240]),
            (RetryPolicy(10, (30, 300)), 10, [30] + [300] * 8),               # the last one repeats
            (RetryPolicy(4, (21600, 21600, 21600)), 4, [21600] * 3),
        ):
            with self.subTest(policy=policy):
                publisher = self.publisher(policy)
                self.assertEqual(self.drive(publisher, self.due()), (sends, delays, "failed"))

    def test_a_send_that_succeeds_on_a_retry_stops_there(self):
        publisher = self.publisher(RetryPolicy(5, (30,)))
        post_id = self.due()
        asyncio.run(publisher.run_due())
        asyncio.run(publisher.run_due(now=time.time() + 31))
        self.platform.error = None
        asyncio.run(publisher.run_due(now=time.time() + 62))
        self.assertEqual((self.platform.sends, self.store.get_post(post_id)["status"]), (3, "published"))
        asyncio.run(publisher.run_due(now=time.time() + 93))
        self.assertEqual(self.platform.sends, 3)


class UncertainNeverRetried(Scheduler):
    """The rule that keeps a post from going out twice lives in code (libs/platforms/base.PlatformError and
    Publisher._send), out of reach of the settings."""

    def test_the_error_class_keeps_uncertain_above_retryable(self):
        self.assertFalse(PlatformError("5xx after sending", retryable=True, uncertain=True).retryable)
        self.assertTrue(PlatformError("5xx after sending", retryable=True, uncertain=True).uncertain)

    def test_unconfirmed_once_under_every_policy(self):
        for policy in (None, RetryPolicy(10, (30,) * 9), RetryPolicy(10, (21600,)), RetryPolicy(1, (30,)), RetryPolicy(2, (30, 60))):
            for error in (PlatformError("timed out after the request was sent", uncertain=True),
                          PlatformError("502 from the platform", retryable=True, uncertain=True),
                          RuntimeError("a plug-in bug while reading the reply")):
                with self.subTest(policy=policy, error=error):
                    publisher = self.publisher(policy)
                    self.platform.error = error
                    post_id = self.due()
                    with self.assertLogs("inkcast.libs.pipeline", "WARNING"):
                        asyncio.run(publisher.run_due())
                    self.assertEqual((self.platform.sends, self.store.get_post(post_id)["status"]), (1, "unconfirmed"))
                    for later in (31, 21601, 10 * DAY):                   # however long the policy would have waited
                        asyncio.run(publisher.run_due(now=time.time() + later))
                    self.assertEqual((self.platform.sends, self.store.get_post(post_id)["status"]), (1, "unconfirmed"))

    def test_an_error_that_is_not_retryable_fails_at_once_under_every_policy(self):
        for policy in (None, RetryPolicy(10, (30,) * 9)):
            with self.subTest(policy=policy):
                publisher = self.publisher(policy)
                self.platform.error = PlatformError("422: the tags are refused")
                self.assertEqual(self.drive(publisher, self.due()), (1, [], "failed"))


class Posture(unittest.TestCase):
    def check(self, policy: RetryPolicy, environment: str = "production"):
        cfg = by_hand(environment=environment, publish_retry=policy)
        return {c.id: c for c in run_checks(cfg, redactor=Redactor(), environ={})}["publish-retry-window"]

    def test_a_window_within_24_hours_passes_and_says_how_long(self):
        for policy, said in ((TODAY, "36 min"), (RetryPolicy(1, (30,)), "no retry"), (RetryPolicy(5, (21600,)), "24 h"),
                             (RetryPolicy(10, (30,) * 9), "4 min 30 s")):
            for environment in ("production", "staging", "development"):
                with self.subTest(policy=policy, environment=environment):
                    check = self.check(policy, environment)
                    self.assertEqual(check.status, "pass", check.detail)
                    self.assertIn(said, check.detail)
                    self.assertIn(f"{ATTEMPTS}={policy.max_attempts}", check.detail)
                    self.assertIn(f"{DELAYS}={','.join(map(str, policy.delays))}", check.detail)

    def test_a_window_over_24_hours_warns_everywhere(self):
        for policy, said in ((RetryPolicy(6, (21600,)), "30 h"), (RetryPolicy(10, (21600,) * 9), "54 h"),
                             (RetryPolicy(7, (3600, 21600)), "31 h"), (RetryPolicy(10, (30, 21600)), "48 h")):
            for environment in ("production", "staging", "ci", "development"):
                with self.subTest(policy=policy, environment=environment):
                    check = self.check(policy, environment)
                    self.assertEqual(check.status, "warn", check.detail)
                    self.assertIn(said, check.detail)
                    self.assertIn(ATTEMPTS, check.detail)
                    self.assertIn(DELAYS, check.detail)

    def test_exactly_24_hours_does_not_exceed_it(self):
        policy = RetryPolicy(5, (21600,))
        self.assertEqual(policy.window_seconds, DAY)
        self.assertEqual(self.check(policy).status, "pass")
        over = RetryPolicy(5, (21599, 21600, 21600, 21600))
        self.assertEqual(over.window_seconds, DAY - 1)
        self.assertEqual(self.check(over).status, "pass")

    def test_the_launcher_logs_the_warning_once(self):
        from inkcast.launcher import gate, resolve
        cfg = by_hand(environment="staging", log_format="json", authz_allow_local=True, publish_retry=RetryPolicy(10, (21600,)))
        with self.assertLogs("inkcast", level="WARNING") as logs:
            gate(cfg, resolve("SchedulerWorker"))
        warned = [r for r in logs.records if getattr(r, "check", "") == "publish-retry-window"]
        self.assertEqual(len(warned), 1)
        self.assertEqual(warned[0].event, "config.posture.warn")


if __name__ == "__main__":
    unittest.main()
