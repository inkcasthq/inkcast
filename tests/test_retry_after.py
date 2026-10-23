# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #126: a 429's ``Retry-After`` sets the retry delay, capped by the policy.

When a platform answers 429 with ``Retry-After``, the scheduler waits ``max(policy delay, Retry-After)``, and never
more than the policy's largest delay (INKCAST_PUBLISH_RETRY_DELAYS), so a hostile value can't park a post for a week.
Both forms of the header are read, delay-seconds and an HTTP-date; anything else, a negative value or a date already
past falls back to the policy's own delay. Which errors are retryable does not change: an uncertain send is never
retried, ``Retry-After`` or not.

What the fix adds (``base.retry_after_seconds``, ``PlatformError(retry_after=...)``, ``delay_after(asked=...)``) is
reached inside the tests, never at import, so against the code before the fix each test runs and fails on its own."""

import asyncio
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx2

from inkcast.libs.config import RetryPolicy
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult, base
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.libs.platforms.medium import MediumPlatform

DEFAULT = RetryPolicy()                    # 4 sends; 60, 300, 1800
WEEK = 7 * 24 * 3600
DAY = 24 * 3600
NOW = 1_800_000_000.0                      # a fixed "now" for the date form: 2027-01-15 08:00:00 UTC
GARBAGE = ("", " ", "soon", "Tomorrow", "1e3", "nan", "inf", "+900", "900.5", "9_00", "0x384", "900s", "900 seconds",
           "٩٠٠", "900, 900", "Wed, 99 Oct 2015 07:28:00 GMT", "Wed, 21 Oct 99999 07:28:00 GMT", "2027-01-15T08:15:00Z")
NEGATIVE = ("-5", "-900", "-0", "-" + "9" * 5000)
HUGE = (str(10 * 365 * DAY), "9" * 4000, "9" * 5000, "Fri, 01 Jan 9999 00:00:00 GMT")  # each is capped like a week


def http_date(seconds_from_now: float, now: float = NOW) -> str:
    """An IMF-fixdate (the HTTP-date form) that many seconds from ``now``, to the second."""
    return format_datetime(datetime.fromtimestamp(int(now) + int(seconds_from_now), timezone.utc), usegmt=True)


class HeaderParsing(unittest.TestCase):
    """``retry_after_seconds`` reads the header the way RFC 9110 §10.2.3 defines it, and refuses everything else."""

    def test_delay_seconds_is_a_non_negative_integer(self):
        for raw, seconds in (("900", 900), ("0", 0), ("1", 1), (" 900 ", 900), ("007", 7), (str(WEEK), WEEK),
                             ("999999999", 999_999_999), ("0" * 5000 + "900", 900)):
            with self.subTest(raw=raw[:20]):
                self.assertEqual(base.retry_after_seconds(raw, now=NOW), seconds)

    def test_a_longer_ask_than_the_ceiling_reads_as_the_ceiling_however_many_digits(self):
        """Thousands of digits are a well-formed delay-seconds: a very long ask, capped like a week, not garbage."""
        self.assertGreater(base.RETRY_AFTER_CEILING, 30 * 365 * DAY, "past any policy's cap (6 h at most)")
        for raw in ("1000000000", "9" * 4000, "9" * 5000, "9" * 16000):
            with self.subTest(digits=len(raw)):
                self.assertEqual(base.retry_after_seconds(raw, now=NOW), base.RETRY_AFTER_CEILING)
        self.assertEqual(base.retry_after_seconds("Fri, 01 Jan 9999 00:00:00 GMT", now=NOW), base.RETRY_AFTER_CEILING)

    def test_an_http_date_is_measured_from_now_in_each_of_the_three_forms(self):
        fifteen_minutes = datetime(2027, 1, 15, 8, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(fifteen_minutes.timestamp(), NOW + 900, "the fixture's 'now' is 08:00:00 UTC that day")
        for raw in ("Fri, 15 Jan 2027 08:15:00 GMT",              # IMF-fixdate, the preferred form
                    "Friday, 15-Jan-27 08:15:00 GMT",             # the obsolete RFC 850 form
                    "Fri Jan 15 08:15:00 2027"):                  # the obsolete asctime form (always UTC)
            with self.subTest(raw=raw):
                self.assertEqual(base.retry_after_seconds(raw, now=NOW), 900)
        self.assertEqual(base.retry_after_seconds("Fri, 15 Jan 2027 13:45:00 +0530", now=NOW), 900, "a zone offset counts")
        self.assertEqual(base.retry_after_seconds("Fri, 15 Jan 2027 08:15:00 GMT", now=NOW + 0.4), 900,
                         "a fraction of a second rounds up, never down")
        self.assertEqual(base.retry_after_seconds("Fri, 15 Jan 2027 08:15:00 GMT", now=NOW - 0.4), 901)

    def test_a_date_already_past_is_zero_the_platform_is_ready(self):
        self.assertEqual(base.retry_after_seconds("Fri, 15 Jan 2027 07:59:00 GMT", now=NOW), 0)
        self.assertEqual(base.retry_after_seconds("Fri, 15 Jan 2027 08:00:00 GMT", now=NOW), 0)
        self.assertEqual(base.retry_after_seconds("Sun, 06 Nov 1994 08:49:37 GMT", now=NOW), 0)

    def test_garbage_negative_and_no_header_are_none(self):
        self.assertIsNone(base.retry_after_seconds(None, now=NOW))
        for raw in GARBAGE + NEGATIVE:
            with self.subTest(raw=raw[:20]):
                self.assertIsNone(base.retry_after_seconds(raw, now=NOW))


class ErrorCarriesTheAsk(unittest.TestCase):
    def test_a_retryable_error_carries_retry_after_and_a_plain_one_carries_none(self):
        exc = PlatformError("429: slow down", retryable=True, retry_after=900)
        self.assertEqual((exc.retryable, exc.uncertain, exc.retry_after), (True, False, 900))
        self.assertIsNone(PlatformError("429: slow down", retryable=True).retry_after)
        self.assertIsNone(PlatformError("422: refused", retry_after=900).retry_after, "nothing to wait for: no retry")

    def test_an_uncertain_error_never_carries_one_whatever_was_passed(self):
        exc = PlatformError("503 after sending", retryable=True, uncertain=True, retry_after=900)
        self.assertEqual((exc.retryable, exc.uncertain, exc.retry_after), (False, True, None))


class Limited(Platform):
    """Answers every publish with the error it is told to (a 429 by default), and counts the sends."""

    id, name, default_api_url, credential_fields = "limited", "Limited", "https://limited.invalid", []

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sends = 0
        self.error: BaseException | None = PlatformError("Limited rate limit hit; try again shortly.", retryable=True)

    async def verify(self, creds):
        return Account(id="1", name="limited")

    def prepare(self, fields, body):
        return Prepared(platform="limited", title=fields["title"], payload={}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sends += 1
        if self.error is not None:
            raise self.error
        return PublishResult(url="https://limited/1", remote_id="1", status="published")


def wire(headers: dict[str, str]) -> list[tuple[bytes, bytes]]:
    """Headers as they come off the wire (bytes), so a value outside ASCII reaches the parser like a real reply's."""
    return [(name.encode("ascii"), value.encode("utf-8")) for name, value in headers.items()]


def rate_limited(headers: dict[str, str], status: int = 429):
    """A DEV API that answers every article POST with ``status`` and these headers, and counts the sends."""
    sends: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and request.url.path == "/api/articles":
            sends.append(request)
            return httpx2.Response(status, headers=wire(headers), json={"error": "Rate limit reached"})
        return httpx2.Response(404, json={"error": "not found"})

    return DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(handler))), sends


class Scheduler(unittest.TestCase):
    """A scheduled post on a platform that keeps failing, ticked whenever it is due."""

    def publisher(self, platform: Platform, policy: RetryPolicy | None = None) -> Publisher:
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        self.user = self.store.upsert_user("local:usr", "usr", None, None)["id"]
        self.platform = platform
        self.store.save_credentials(self.user, platform.id, {"api_key": "k", "k": "v"}, {"id": "1", "name": platform.id})
        if policy is None:
            return Publisher(self.store, {platform.id: platform})            # the default policy
        return Publisher(self.store, {platform.id: platform}, retry=policy)

    def due(self, run_at: float | None = None) -> str:
        prepared = self.platform.prepare({"title": "t"}, "Body.")
        return self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title="t",
                                      status="scheduled", run_at=time.time() - 1 if run_at is None else run_at,
                                      payload=prepared.to_dict())

    def wait_after_one_tick(self, publisher: Publisher, post_id: str) -> float:
        """Tick once: how long the post was told to wait before its retry (it must still be scheduled)."""
        asyncio.run(publisher.run_due())
        post = self.store.get_post(post_id)
        self.assertEqual((post["status"], post["attempts"]), ("scheduled", 1))
        return post["run_at"] - post["updated_at"]

    def drive(self, publisher: Publisher, post_id: str) -> tuple[str, list[int]]:
        """Tick whenever the post is due: (the final status, the delay before each retry)."""
        delays: list[int] = []
        for _ in range(40):
            post = self.store.get_post(post_id)
            if post["status"] != "scheduled":
                break
            asyncio.run(publisher.run_due(now=post["run_at"] + 1))
            post = self.store.get_post(post_id)
            if post["status"] == "scheduled":                    # rescheduled: run_at was set to now + delay
                delays.append(round(post["run_at"] - post["updated_at"]))
        return self.store.get_post(post_id)["status"], delays


class TheWaitFollowsRetryAfter(Scheduler):
    def test_a_429_with_retry_after_900_under_the_default_policy_retries_after_900_s_not_60(self):
        devto, sends = rate_limited({"Retry-After": "900"})
        publisher = self.publisher(devto)
        post_id = self.due()
        self.assertAlmostEqual(self.wait_after_one_tick(publisher, post_id), 900, delta=2)
        self.assertEqual(len(sends), 1)
        # Not due at the policy's 60 s, nor at 899 s; due at 900 s.
        for early in (61, 899):
            asyncio.run(publisher.run_due(now=time.time() + early))
            self.assertEqual(len(sends), 1, f"retried {early} s after a 429 that asked for 900")
        asyncio.run(publisher.run_due(now=time.time() + 901))
        self.assertEqual(len(sends), 2)

    def test_the_ask_is_the_longer_of_the_two_at_every_attempt(self):
        """Default policy, every 429 asking for 900 s: 900 beats 60 and 300, and the policy's 1800 beats 900."""
        devto, sends = rate_limited({"Retry-After": "900"})
        publisher = self.publisher(devto)
        self.assertEqual(self.drive(publisher, self.due()), ("failed", [900, 900, 1800]))
        self.assertEqual(len(sends), 4, "the attempt count is the policy's")

    def test_an_ask_shorter_than_the_policy_delay_changes_nothing(self):
        for raw in ("0", "1", "5", "59"):
            with self.subTest(raw=raw):
                devto, _ = rate_limited({"Retry-After": raw})
                publisher = self.publisher(devto)
                self.assertEqual(self.drive(publisher, self.due()), ("failed", [60, 300, 1800]))

    def test_an_http_date_works(self):
        devto, sends = rate_limited({"Retry-After": http_date(900, now=time.time())})
        publisher = self.publisher(devto)
        self.assertAlmostEqual(self.wait_after_one_tick(publisher, self.due()), 900, delta=2)
        asyncio.run(publisher.run_due(now=time.time() + 61))
        self.assertEqual(len(sends), 1, "retried at the policy's 60 s although the date was 15 minutes away")
        asyncio.run(publisher.run_due(now=time.time() + 901))
        self.assertEqual(len(sends), 2)

    def test_a_week_is_capped_at_the_policy_s_largest_delay(self):
        devto, sends = rate_limited({"Retry-After": str(WEEK)})
        publisher = self.publisher(devto)
        self.assertEqual(self.drive(publisher, self.due()), ("failed", [1800, 1800, 1800]))
        self.assertEqual(len(sends), 4)
        for raw in (http_date(WEEK, now=time.time()),) + HUGE:
            with self.subTest(raw=raw[:30]):
                devto, _ = rate_limited({"Retry-After": raw})
                publisher = self.publisher(devto)
                self.assertAlmostEqual(self.wait_after_one_tick(publisher, self.due()), 1800, delta=2)

    def test_the_cap_is_the_largest_delay_of_the_policy_in_force(self):
        for policy, asked, delays in (
            (RetryPolicy(3, (45, 90)), "60", [60, 90]),                       # 60 beats 45; 90 beats 60
            (RetryPolicy(3, (45, 90)), "100", [90, 90]),                      # 100 is capped at 90
            (RetryPolicy(3, (45, 90)), str(WEEK), [90, 90]),
            (RetryPolicy(2, (30,)), "31", [30]),                              # one delay: it is the cap too
            (RetryPolicy(4, (21600, 21600, 21600)), str(WEEK), [21600] * 3),  # the ceiling of a delay is the most
            (RetryPolicy(5, (30, 60, 120, 240)), "100", [100, 100, 120, 240]),
        ):
            with self.subTest(policy=policy, asked=asked):
                devto, _ = rate_limited({"Retry-After": asked})
                publisher = self.publisher(devto, policy)
                self.assertEqual(self.drive(publisher, self.due()), ("failed", delays))

    def test_garbage_and_negative_values_fall_back_to_the_policy_delay(self):
        for raw in GARBAGE + NEGATIVE + (http_date(-60, now=time.time()), "Sun, 06 Nov 1994 08:49:37 GMT"):
            with self.subTest(raw=raw[:30]):
                devto, sends = rate_limited({"Retry-After": raw})
                publisher = self.publisher(devto)
                self.assertAlmostEqual(self.wait_after_one_tick(publisher, self.due()), 60, delta=2)
                self.assertEqual(len(sends), 1)
        devto, _ = rate_limited({})                                           # no header at all: as before
        publisher = self.publisher(devto)
        self.assertEqual(self.drive(publisher, self.due()), ("failed", [60, 300, 1800]))

    def test_a_post_published_now_is_still_not_retried(self):
        """Retry-After doesn't make a 'now' post retry: only scheduled posts are, as before."""
        from inkcast.libs.content.story import parse_story
        devto, sends = rate_limited({"Retry-After": "900"})
        publisher = self.publisher(devto)
        story = parse_story("# Now\n\nBody.\n", known_platforms=["devto"])
        (outcome,) = asyncio.run(publisher.publish(self.user, story, ["devto"]))
        self.assertEqual((outcome.status, len(sends)), ("failed", 1))
        asyncio.run(publisher.run_due(now=time.time() + 901))
        self.assertEqual(len(sends), 1)


class PolicyArithmetic(unittest.TestCase):
    """``RetryPolicy.delay_after``: the policy alone is what it was; an ask lengthens a wait, within the largest delay."""

    def test_the_policy_alone_is_unchanged(self):
        self.assertEqual([DEFAULT.delay_after(a) for a in (1, 2, 3, 4, 9)], [60, 300, 1800, 1800, 1800])
        self.assertEqual(DEFAULT.window_seconds, 60 + 300 + 1800)

    def test_the_wait_is_the_longer_of_the_two_never_past_the_largest_delay(self):
        self.assertEqual([DEFAULT.delay_after(a, asked=None) for a in (1, 2, 3)], [60, 300, 1800])
        self.assertEqual([DEFAULT.delay_after(1, asked=a) for a in (0, 59, 60, 61, 900, 1800, 1801, WEEK)],
                         [60, 60, 60, 61, 900, 1800, 1800, 1800])
        self.assertEqual(DEFAULT.delay_after(1, asked=60.5), 61, "a fraction waits the whole second")
        self.assertEqual(DEFAULT.delay_after(1, asked=10 ** 5000), 1800, "an integer too big for a float is capped too")
        self.assertEqual(RetryPolicy(3, (45, 90)).delay_after(1, asked=10 ** 5000), 90)

    def test_an_ask_that_is_not_a_usable_number_is_ignored(self):
        for bad in (-1, -900.0, float("nan"), float("inf"), float("-inf"), True, "900", None):
            with self.subTest(bad=bad):
                self.assertEqual(DEFAULT.delay_after(1, asked=bad), 60)


class TheHelperReadsTheHeader(unittest.TestCase):
    """``Platform.request`` is where a 429 becomes a retryable error, for every built-in platform; the header is read
    there, and what is retryable or uncertain stays exactly as it was."""

    def platforms(self, status: int, headers: dict[str, str]):
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(status, headers=wire(headers), json={"error": "no"})
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        return DevtoPlatform(http), MediumPlatform(http)

    def send(self, platform: Platform) -> PlatformError:
        creds = {"api_key": "k", "token": "t"}
        prepared = platform.prepare({"title": "t"}, "Body.")
        with self.assertRaises(PlatformError) as caught:
            asyncio.run(platform.publish(creds, Account(id="1", name="n"), prepared, None))
        return caught.exception

    def test_a_429_s_retry_after_rides_on_the_retryable_error_in_both_forms(self):
        for headers, seconds in (({"Retry-After": "900"}, 900), ({"retry-after": "900"}, 900),
                                 ({"Retry-After": http_date(900, now=time.time())}, 900), ({}, None),
                                 ({"Retry-After": "soon"}, None), ({"Retry-After": "-5"}, None)):
            for platform in self.platforms(429, headers):
                with self.subTest(platform=platform.id, headers=headers):
                    exc = self.send(platform)
                    self.assertEqual((exc.retryable, exc.uncertain), (True, False))
                    if seconds is None:
                        self.assertIsNone(exc.retry_after)
                    else:
                        self.assertAlmostEqual(exc.retry_after, seconds, delta=2)
                    self.assertIn("rate limit", str(exc))

    def test_a_5xx_on_a_publish_stays_uncertain_whatever_retry_after_says(self):
        for status in (500, 502, 503):
            for platform in self.platforms(status, {"Retry-After": "30"}):
                with self.subTest(platform=platform.id, status=status):
                    exc = self.send(platform)
                    self.assertEqual((exc.retryable, exc.uncertain), (False, True))
                    self.assertIsNone(getattr(exc, "retry_after", None))

    def test_a_4xx_rejection_with_retry_after_is_still_final(self):
        for platform in self.platforms(422, {"Retry-After": "30"}):
            with self.subTest(platform=platform.id):
                exc = self.send(platform)
                self.assertEqual((exc.retryable, exc.uncertain), (False, False))
                self.assertIsNone(getattr(exc, "retry_after", None))


class UncertainStillNeverRetried(Scheduler):
    """The rule that keeps a post from going out twice is untouched: a send that may have reached the platform is
    ``unconfirmed`` once, and Retry-After, however small or large, changes nothing."""

    def test_a_503_with_retry_after_on_the_real_helper_is_unconfirmed_not_retried(self):
        for asked in ("30", "900", str(WEEK)):
            with self.subTest(asked=asked):
                devto, sends = rate_limited({"Retry-After": asked}, status=503)
                publisher = self.publisher(devto)
                post_id = self.due()
                with self.assertLogs("inkcast.libs.pipeline", "WARNING"):
                    asyncio.run(publisher.run_due())
                self.assertEqual((len(sends), self.store.get_post(post_id)["status"]), (1, "unconfirmed"))
                for later in (31, 61, 901, 1801, WEEK + 1):
                    asyncio.run(publisher.run_due(now=time.time() + later))
                self.assertEqual((len(sends), self.store.get_post(post_id)["status"]), (1, "unconfirmed"))

    def test_an_uncertain_error_with_retry_after_is_unconfirmed_once_under_every_policy(self):
        for policy in (None, RetryPolicy(10, (30,) * 9), RetryPolicy(10, (21600,)), RetryPolicy(2, (30, 60))):
            for error in (PlatformError("timed out after the request was sent", uncertain=True, retry_after=1),
                          PlatformError("503 from the platform", retryable=True, uncertain=True, retry_after=900),
                          PlatformError("503 from the platform", retryable=True, uncertain=True, retry_after=WEEK)):
                with self.subTest(policy=policy, error=error):
                    limited = Limited()
                    limited.error = error
                    publisher = self.publisher(limited, policy)
                    post_id = self.due()
                    with self.assertLogs("inkcast.libs.pipeline", "WARNING"):
                        asyncio.run(publisher.run_due())
                    self.assertEqual((limited.sends, self.store.get_post(post_id)["status"]), (1, "unconfirmed"))
                    for later in (2, 31, 901, 21601, WEEK + 1, 10 * DAY):
                        asyncio.run(publisher.run_due(now=time.time() + later))
                    self.assertEqual((limited.sends, self.store.get_post(post_id)["status"]), (1, "unconfirmed"))

    def test_the_error_class_keeps_uncertain_above_retryable(self):
        exc = PlatformError("5xx after sending", retryable=True, uncertain=True, retry_after=5)
        self.assertEqual((exc.retryable, exc.uncertain, exc.retry_after), (False, True, None))


if __name__ == "__main__":
    unittest.main()
