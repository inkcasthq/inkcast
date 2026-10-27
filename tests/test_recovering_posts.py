# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #216: a 'recovering' post can be cancelled, raises an alert after an hour, and is capped.

Since #199 a post whose send was interrupted stays 'recovering' for as long as the platform refuses the lookup that
would tell whether it arrived: asked again every stuck pass, with no end, no alert, and no way for a person to stop
it; when the platform finally said "missing", the post was sent, possibly days late. Now:

* a person can cancel a 'recovering' post (the API, the MCP tool and Git sync's cancel all go through
  ``Publisher.cancel``), and a cancel wins over a recovery answer that lands after it;
* ``inkcast_posts_recovering_oldest_age_seconds`` carries how long the oldest 'recovering' post has been so, and
  ``InkcastPostRecoveringTooLong`` fires past an hour;
* after INKCAST_PUBLISH_RECOVERY_MAX_HOURS (24 by default, 1 to 168) recovery stops and the post is 'unconfirmed':
  it is never sent late without a person deciding.

Each ``Finding`` test failed on main before the fix committed with it; the ``Held`` tests pin what stays."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import httpx2
import yaml

from inkcast.libs.alerting import ALERTS
from inkcast.libs.config import Config, ConfigError, load_config
from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.db.ledger import CLAIM_TTL
from inkcast.libs.observability import Metrics
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult
from inkcast.libs.runtime import build_context, build_metrics
from inkcast.services.publishing.sync import GitSync
from inkcast.services.publishing.usecases import InkcastService, ServiceError

ROOT = Path(__file__).resolve().parents[1]
HOUR, DAY = 3600, 86400
SETTING = "INKCAST_PUBLISH_RECOVERY_MAX_HOURS"
BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
        "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}


def config(**env) -> Config:
    return load_config({**BASE, **env})


def by_hand(**fields) -> Config:
    return Config(**{"public_url": "http://localhost:8787", "secret_key": "k" * 40, "database_path": ":memory:",
                     "login": "password", "password": "p" * 12, **fields})


class Findable(Platform):
    """A platform that can be checked (``find``). ``refuse`` makes every lookup fail for a reason that will pass (a
    429), as a rate-limiting platform does; otherwise it finds what it published. ``meanwhile(prepared)`` (an async
    hook) runs inside each lookup while set, after the platform has answered and before the caller sees the answer;
    it unsets itself when it is done."""

    id, name, default_api_url, credential_fields = "findable", "Findable", "https://findable.invalid", []
    capabilities = frozenset({"find"})

    def __init__(self):
        super().__init__(httpx2.AsyncClient())
        self.sent: list[str] = []
        self.lookups = 0
        self.refuse = True
        self.meanwhile = None
        self.on_publish = None   # an async hook run inside publish(), after the request "reached" the platform

    async def verify(self, creds):
        return Account(id="1", name="findable")

    def prepare(self, fields, body):
        return Prepared(platform=self.id, title=fields["title"], payload={"body": body}, preview={})

    async def publish(self, creds, account, prepared, destination):
        self.sent.append(prepared.title)
        if self.on_publish:
            await self.on_publish(prepared)
        return PublishResult(url=f"https://findable/{len(self.sent)}", remote_id=str(len(self.sent)), status="published")

    async def find_existing(self, creds, account, prepared):
        self.lookups += 1
        if self.refuse:
            raise PlatformError("Findable rate limit hit; try again in 15 minutes.", retryable=True, retry_after=900)
        answer = (PublishResult(url="https://findable/found", remote_id="found", status="published")
                  if prepared.title in self.sent else None)
        if self.meanwhile:
            await self.meanwhile(prepared)
        return answer


class Harness(unittest.TestCase):
    """One database, opened by the scheduler (``publisher``) and, where a test needs it, by another process."""

    def setUp(self):
        self.path = tempfile.mkdtemp() + "/inkcast.db"
        self.store = self.open()
        self.user = self.store.upsert_user("local:on-call", "on-call", None, None)["id"]
        self.platform = Findable()
        self.store.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "findable"})
        self.publisher = Publisher(self.store, {self.platform.id: self.platform})

    def open(self) -> Store:
        store = Store(self.path, "k" * 40)
        self.addCleanup(store.close)
        return store

    def stuck(self, title: str = "t") -> str:
        """A post the process was sending when it stopped: 'publishing' for longer than STUCK_AFTER."""
        prepared = self.platform.prepare({"title": title}, "Body.")
        post_id = self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title=title,
                                         status="publishing", run_at=time.time() - 1, payload=prepared.to_dict())
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        return post_id

    def recovering(self, title: str = "t") -> str:
        """A post whose recovery the platform has refused once: 'recovering', asked again next stuck pass."""
        post_id = self.stuck(title)
        self.tick()
        self.assertEqual(self.status(post_id), "recovering")
        return post_id

    def tick(self, later: float = 0.0, publisher: Publisher | None = None):
        """One scheduler tick ``later`` seconds from now; a stuck pass when ``later`` is past STUCK_AFTER."""
        publisher = publisher or self.publisher
        with self.assertLogs("inkcast.libs.pipeline", "WARNING"):
            return asyncio.run(publisher.run_due(now=time.time() + later))

    def status(self, post_id: str) -> str:
        return self.store.get_post(post_id)["status"]

    def events(self, post_id: str) -> list[tuple[str, str | None]]:
        return [(e.type, e.attrs.get("status")) for e in self.store.poll("audit", limit=100) if e.subject == post_id]


# ---- a person can cancel a recovering post -------------------------------------------------------------------


class FindingARecoveringPostCanBeCancelled(Harness):
    def test_the_cancel_is_accepted_and_the_post_stays_cancelled_whatever_the_platform_says_later(self):
        post_id = self.recovering()
        self.assertEqual(self.publisher.cancel(self.user, post_id), "t")
        self.assertEqual(self.status(post_id), "cancelled")
        self.assertIn(("post.cancelled", None), self.events(post_id))
        # The platform answers at last, "missing": the post is not re-queued, and nothing is sent.
        self.platform.refuse = False
        for later in (STUCK_AFTER + 1, 2 * (STUCK_AFTER + 1)):
            asyncio.run(self.publisher.run_due(now=time.time() + later))
        self.assertEqual((self.status(post_id), self.platform.sent), ("cancelled", []))
        self.assertNotIn("post.recovered", [t for t, _ in self.events(post_id)])

    def test_the_mcp_tool_and_the_rest_route_share_the_use_case_and_it_cancels_too(self):
        post_id = self.recovering()
        service = InkcastService(self.store, {self.platform.id: self.platform}, self.publisher, "https://inkcast.example.com")
        info = service.cancel(self.user, post_id)
        self.assertEqual((info.id, info.status), (post_id, "cancelled"))
        with self.assertRaises(ServiceError) as caught:                       # a second cancel: nothing to do
            service.cancel(self.user, post_id)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_cancellable"))
        self.assertIn("is cancelled", caught.exception.message)

    def test_the_refusal_names_what_can_be_cancelled(self):
        post_id = self.stuck()                                                # 'publishing': being sent right now
        with self.assertRaisesRegex(PlatformError, "is publishing; only scheduled or recovering posts can be cancelled"):
            self.publisher.cancel(self.user, post_id)
        self.assertEqual(self.status(post_id), "publishing")
        self.assertFalse(self.store.cancel_if_pending(post_id, "someone-else"))


class FindingACancelWinsOverALateRecoveryAnswer(Harness):
    """Two processes: the scheduler (worker A) is recovering the post, its lookup out at the platform, and the API
    (process B, its own connection) cancels it meanwhile. The platform's answer is "missing", the one that re-queues:
    written after the cancel, it would put a cancelled post back in the queue and send it."""

    def test_the_answer_that_lands_after_the_cancel_does_not_re_queue_the_post(self):
        post_id = self.recovering()
        api = Publisher(self.open(), {self.platform.id: self.platform})
        self.platform.refuse = False

        refused: list[Exception] = []

        async def the_person_cancels(prepared):
            self.platform.meanwhile = None
            try:
                api.cancel(self.user, post_id)
            except PlatformError as exc:   # confirm() would swallow it as "the platform can't tell"
                refused.append(exc)

        self.platform.meanwhile = the_person_cancels
        self.tick(later=STUCK_AFTER + 1)                                      # A's pass: the lookup, the cancel, A's write
        self.assertEqual(refused, [], "the cancel was refused")
        self.assertEqual(self.status(post_id), "cancelled")
        asyncio.run(self.publisher.run_due(now=time.time() + 2 * (STUCK_AFTER + 1)))   # the next tick
        self.assertEqual((self.status(post_id), self.platform.sent), ("cancelled", []), "sent after the cancel")
        self.assertEqual(self.events(post_id), [("post.cancelled", None)])


class HeldWhatACancelStillDoes(Harness):
    def test_a_cancel_after_the_re_queue_still_stops_the_send(self):
        """The other order: recovery re-queued the post (the platform said "missing") a moment before the cancel,
        while the same pass was still looking up the next stuck post. The post is 'scheduled' then, and the cancel
        takes it as it always did; the pass's send of due posts, right after, leaves it alone."""
        first, second = self.recovering("first"), self.recovering("second")
        api = Publisher(self.open(), {self.platform.id: self.platform})
        self.platform.refuse = False

        async def the_person_cancels(prepared):
            if prepared.title != "second":
                return                                                        # not yet: "first" is still being looked up
            self.platform.meanwhile = None
            self.assertEqual(self.status(first), "scheduled")                 # re-queued, due at once
            self.assertEqual(api.cancel(self.user, first), "first")

        self.platform.meanwhile = the_person_cancels
        self.tick(later=STUCK_AFTER + 1)
        self.assertEqual((self.status(first), self.status(second)), ("cancelled", "published"))
        self.assertEqual(self.platform.sent, ["second"], "the cancelled post was sent by the same pass")

    def test_a_post_already_out_is_not_cancellable(self):
        self.platform.refuse = False
        self.platform.sent.append("t")                                        # it had reached the platform
        post_id = self.stuck()
        self.tick(later=STUCK_AFTER + 1)                                      # recovery: found, so 'published'
        self.assertEqual(self.status(post_id), "published")
        with self.assertRaisesRegex(PlatformError, "is published"):
            self.publisher.cancel(self.user, post_id)
        self.assertFalse(self.store.cancel_if_pending(post_id, self.user))


# ---- the cap: recovery ends unconfirmed, never a late send --------------------------------------------------


class FindingRecoveryIsCapped(Harness):
    def test_after_the_cap_the_post_is_unconfirmed_and_never_sent(self):
        self.publisher.metrics = Metrics()
        self.publisher.metrics.counter("inkcast_publish_total", "")
        post_id = self.recovering()
        since = self.store.get_post(post_id)["recovering_since"]
        self.assertAlmostEqual(since, time.time(), delta=5, msg="when it entered 'recovering'")
        self.tick(later=23 * HOUR)                                            # still asked, still refused
        self.assertEqual((self.status(post_id), self.platform.lookups), ("recovering", 2))
        self.assertEqual(self.store.get_post(post_id)["recovering_since"], since, "kept across stuck passes")
        (outcome,) = self.tick(later=DAY + 1)                                 # the cap: not asked again
        post = self.store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], self.platform.lookups), ("unconfirmed", "unconfirmed", 2))
        self.assertIn(SETTING, post["error"])
        self.assertIn("24 hours", post["error"])
        self.assertIn("check Findable before publishing it again", post["error"])
        self.assertIn(("post.recovered", "unconfirmed"), self.events(post_id))
        self.assertEqual(self.publisher.metrics.snapshot()["inkcast_publish_total"],
                         {(("platform", "findable"), ("status", "unconfirmed")): 1})
        # The platform answers "missing" from now on: the post is a person's to decide, never sent on its own.
        self.platform.refuse = False
        for later in (DAY + STUCK_AFTER + 2, 2 * DAY):
            asyncio.run(self.publisher.run_due(now=time.time() + later))
        self.assertEqual((self.status(post_id), self.platform.sent, self.platform.lookups), ("unconfirmed", [], 2))

    def test_the_cap_is_the_publishers_setting_in_seconds(self):
        self.publisher.recovery_max = 2 * HOUR
        post_id = self.recovering()
        self.tick(later=2 * HOUR - 60)
        self.assertEqual(self.status(post_id), "recovering")
        self.tick(later=2 * HOUR + 1)
        post = self.store.get_post(post_id)
        self.assertEqual(post["status"], "unconfirmed")
        self.assertIn("2 hours", post["error"])

    def test_a_cancel_before_the_cap_holds_through_it(self):
        post_id = self.recovering()
        self.publisher.cancel(self.user, post_id)
        asyncio.run(self.publisher.run_due(now=time.time() + DAY + 1))
        self.assertEqual(self.status(post_id), "cancelled")

    def test_a_post_recovered_again_after_a_second_interruption_starts_its_clock_afresh(self):
        """Recovered as missing, re-queued and sent by the same pass, which the process stops in: the second
        recovery is measured from its own start, not the first's."""
        class Died(BaseException):
            """The process stopped right here: nothing after this line runs."""

        async def died(prepared):
            raise Died()

        post_id = self.recovering()
        self.platform.refuse = False
        self.platform.on_publish = died
        with self.assertRaises(Died):
            asyncio.run(self.publisher.run_due(now=time.time() + STUCK_AFTER + 1))   # missing → re-queued → sent → died
        self.platform.on_publish = None
        self.assertEqual((self.status(post_id), self.platform.sent), ("publishing", ["t"]))
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?", STUCK_AFTER + 1)
        self.platform.refuse = True
        self.tick(later=20 * HOUR)                                            # the stuck pass that takes it, 20 h on
        self.assertEqual(self.status(post_id), "recovering")
        self.assertAlmostEqual(self.store.get_post(post_id)["recovering_since"], time.time() + 20 * HOUR, delta=5)
        self.tick(later=DAY + 1)                                              # 4 hours into this recovery: not capped
        self.assertEqual(self.status(post_id), "recovering")
        self.tick(later=DAY + 20 * HOUR + 1)                                  # 24 hours into it: capped
        self.assertEqual(self.status(post_id), "unconfirmed")

    def test_the_context_hands_the_configured_cap_to_the_publisher(self):
        cfg = by_hand(database_path=self.path, publish_recovery_max_hours=6)
        ctx = build_context(cfg)
        self.addCleanup(ctx.store.close)
        self.assertEqual(ctx.publisher.recovery_max, 6 * HOUR)
        self.assertEqual(Publisher(self.store, {}).recovery_max, 24 * HOUR, "the default is the setting's default")


# ---- the alert ---------------------------------------------------------------------------------------------


class FindingTheAlert(Harness):
    def gauge(self) -> float:
        metrics = build_metrics(self.store, {}, by_hand())
        return metrics.snapshot()["inkcast_posts_recovering_oldest_age_seconds"][()]

    def test_the_gauge_is_how_long_the_oldest_recovering_post_has_been_so(self):
        self.assertEqual(self.gauge(), 0.0)
        self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title="sending",
                               status="publishing", run_at=time.time(), payload={})   # being sent: not 'recovering'
        self.assertEqual(self.gauge(), 0.0)
        first, second = self.recovering("a"), self.recovering("b")
        self.store._exec("UPDATE posts SET recovering_since = recovering_since - ? WHERE id = ?", 2 * HOUR, first)
        self.store._exec("UPDATE posts SET recovering_since = recovering_since - ? WHERE id = ?", HOUR, second)
        self.assertAlmostEqual(self.gauge(), 2 * HOUR, delta=5)
        self.store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE status = 'recovering'", STUCK_AFTER + 1)
        self.tick()                                                           # asked again: the age is not reset
        self.assertAlmostEqual(self.gauge(), 2 * HOUR, delta=5)
        self.publisher.cancel(self.user, first)
        self.assertAlmostEqual(self.gauge(), HOUR, delta=5)
        self.publisher.cancel(self.user, second)
        self.assertEqual(self.gauge(), 0.0)

    def test_a_row_from_before_the_column_counts_from_its_last_change(self):
        post_id = self.stuck()
        self.store._exec("UPDATE posts SET status = 'recovering', recovering_since = NULL WHERE id = ?", post_id)
        self.assertAlmostEqual(self.gauge(), STUCK_AFTER + 1, delta=5)

    def test_the_rule_fires_past_an_hour_and_is_generated(self):
        (alert,) = [a for a in ALERTS if a.name == "InkcastPostRecoveringTooLong"]
        self.assertEqual(alert.expr, "max(inkcast_posts_recovering_oldest_age_seconds) > 3600")
        self.assertEqual((alert.severity, alert.service, alert.for_), ("warning", "scheduler-worker", "0m"))
        self.assertIn(SETTING, alert.description)
        rules = yaml.safe_load((ROOT / "deploy/kustomize/components/prometheus-rules/prometheusrule.yaml").read_text())
        names = [r["alert"] for g in rules["spec"]["groups"] for r in g["rules"] if "alert" in r]
        self.assertIn("InkcastPostRecoveringTooLong", names)
        runbook = (ROOT / "docs/runbooks/alerts.md").read_text(encoding="utf-8")
        self.assertIn("## InkcastPostRecoveringTooLong", runbook)
        self.assertIn(SETTING, runbook)


# ---- the setting -------------------------------------------------------------------------------------------


class FindingTheSetting(unittest.TestCase):
    def test_from_1_to_168_hours_and_the_default_is_24(self):
        from inkcast.libs.config import PUBLISH_RECOVERY_MAX_HOURS
        self.assertEqual(PUBLISH_RECOVERY_MAX_HOURS, (1, 168))
        self.assertEqual(config().publish_recovery_max_hours, 24)
        self.assertEqual(config().publish_recovery_max_seconds, 24 * HOUR)
        self.assertEqual(config(INKCAST_PUBLISH_RECOVERY_MAX_HOURS="").publish_recovery_max_hours, 24)
        for raw, value in (("1", 1), (" 12 ", 12), ("168", 168)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_PUBLISH_RECOVERY_MAX_HOURS=raw)
                self.assertEqual((cfg.publish_recovery_max_hours, cfg.publish_recovery_max_seconds), (value, value * HOUR))
                cfg.validate()

    def test_outside_the_range_or_not_a_whole_number_is_a_configuration_error_naming_the_variable(self):
        for raw in ("0", "169", "-24", "24.5", "day", "1e1", "0x18", "24,48", "inf", "nan", "true", "off"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{SETTING} must be a whole number of hours from 1 to 168") as caught:
                    config(INKCAST_PUBLISH_RECOVERY_MAX_HOURS=raw)
                self.assertIn(repr(raw), str(caught.exception), "the error quotes the value it refused")

    def test_a_config_built_by_hand_is_held_to_the_same_rule(self):
        for hours in (1, 24, 168):
            by_hand(publish_recovery_max_hours=hours).validate()
        for hours in (0, 169, 24.0, "24", True, None):
            with self.subTest(hours=hours):
                with self.assertRaisesRegex(ConfigError, f"{SETTING} must be a whole number of hours"):
                    by_hand(publish_recovery_max_hours=hours).validate()
        from inkcast.libs.config import publish_recovery_problems
        self.assertEqual(publish_recovery_problems(24), [])
        self.assertEqual(len(publish_recovery_problems(0)), 1)

    def test_the_docs_and_the_hosted_environments_state_it(self):
        self.assertIn(f"# {SETTING}=24\n", (ROOT / ".env.example").read_text(encoding="utf-8"))
        rows = {line.split("|")[1].strip(): line for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
                if line.startswith("| `INKCAST_")}
        self.assertIn(f"`{SETTING}`", rows)
        self.assertTrue(rows[f"`{SETTING}`"].startswith(f"| `{SETTING}` | `24` |"), rows[f"`{SETTING}`"])
        self.assertIn("`1` to `168`", rows[f"`{SETTING}`"])
        literals = {}
        for environment in ("production", "staging"):
            k = yaml.safe_load((ROOT / "deploy/kustomize/environments" / environment / "kustomization.yaml").read_text())
            pairs = dict(lit.split("=", 1) for g in k.get("configMapGenerator", []) if g["name"] == "inkcast-config"
                         for lit in g.get("literals", []))
            self.assertIn(SETTING, pairs, f"{environment} leaves {SETTING} to the default")
            literals[environment] = pairs[SETTING]
        self.assertEqual(literals["production"], literals["staging"])
        config(INKCAST_PUBLISH_RECOVERY_MAX_HOURS=literals["production"]).validate()


# ---- Git sync ----------------------------------------------------------------------------------------------


class FindingGitSyncDoesNotReplaceARecoveringPost(Harness):
    """A content edit replaces a 'scheduled' post (cancel, then create): safe, it was never sent. A 'recovering' one
    may have reached the platform, so the sync neither replaces it (that could post twice) nor calls it 'stale' as
    if it were published on a platform that can't edit: it says what the post is waiting for, and that cancelling
    the post is how a person decides. After that cancel the file is synced as any cancelled post's: unchanged, it
    stays cancelled; edited, it becomes a new post."""

    SOURCE = "github.com/acme/blog"

    def sync(self, markdown: str):
        git = GitSync(self.store, {self.platform.id: self.platform}, self.publisher)
        req = SyncRequest(source=self.SOURCE, files=[SyncFile(path="posts/p.md", markdown=markdown)])
        (result,) = asyncio.run(git.sync(self.user, req)).results
        return result

    def story(self, body: str = "Body.") -> str:
        return f"---\ntitle: Hello\nplatforms: [findable]\n---\n\n{body}\n"

    def test_an_edit_waits_for_the_person_and_a_cancel_lets_the_next_sync_create_the_new_version(self):
        class Died(BaseException):
            """The process stopped right here: nothing after this line runs."""

        async def died(prepared):
            raise Died()

        self.platform.on_publish = died                   # the request reached the platform; the reply was never read
        with self.assertRaises(Died):
            self.sync(self.story())
        self.platform.on_publish = None
        (post_id,) = [r["id"] for r in self.store._all("SELECT id FROM posts")]
        self.store._exec("UPDATE ledger SET synced_at = synced_at - ?", CLAIM_TTL + 1)
        self.store._exec("UPDATE posts SET updated_at = updated_at - ?", STUCK_AFTER + 1)
        self.tick()                                       # the scheduler's recovery: the platform refuses the lookup
        self.assertEqual(self.status(post_id), "recovering")
        unchanged = self.sync(self.story())
        self.assertEqual((unchanged.action, unchanged.status, unchanged.post_id), ("unchanged", "recovering", post_id))
        edited = self.sync(self.story("Better body."))
        self.assertEqual((edited.action, edited.status, edited.post_id), ("skipped", "recovering", post_id))
        self.assertIn("interrupted", edited.message)
        self.assertIn("cancel", edited.message)
        self.assertNotIn("doesn't let Inkcast edit posts", edited.message)
        self.assertEqual((self.status(post_id), self.platform.sent), ("recovering", ["Hello"]), "replaced")
        # The person decides: cancels it. The edited file is then a new post; an unchanged one stays cancelled.
        self.publisher.cancel(self.user, post_id)
        again = self.sync(self.story())
        self.assertEqual((again.action, again.status), ("unchanged", "cancelled"))
        new = self.sync(self.story("Better body."))
        self.assertEqual((new.action, new.status), ("create", "published"))
        self.assertNotEqual(new.post_id, post_id)
        self.assertEqual(self.platform.sent, ["Hello", "Hello"])


if __name__ == "__main__":
    unittest.main()
