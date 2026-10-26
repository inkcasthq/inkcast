"""Domain events and the transactional outbox: written with the row change, read in order per consumer,
replayed safely, catalogued."""

import pathlib
import re
import time
import unittest

from fakes import seed_people

from inkcast.libs.db import Store
from inkcast.libs.events import EVENT_TYPES, DomainEvent, MemorySink
from inkcast.libs.outbox import OutboxPoller

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "inkcast"


class FlakySink(MemorySink):
    name = "flaky"

    def __init__(self, fail_times: int):
        super().__init__()
        self.fail_times = fail_times

    def handle(self, events):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("downstream is away")
        super().handle(events)


class Outbox(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", "k" * 40)
        self.addCleanup(self.store.close)
        seed_people(self.store, "usr")          # a row for a person without a users row is refused (#196)

    def post(self, status="scheduled", run_at=None):
        return self.store.create_post(group_id="g", user_id="usr", platform="devto", title="t", status=status,
                                      run_at=run_at or time.time() + 60, payload={})

    def test_events_are_written_with_the_change_and_read_in_order(self):
        pid = self.post()                                                    # post.scheduled
        self.store.update_post(pid, status="published", events=[
            DomainEvent("post.published", user_id="usr", subject=pid, attrs={"platform": "devto", "status": "published"})])
        self.post(status="publishing")                                       # no event: the send that follows emits one
        events = self.store.poll("insights")
        self.assertEqual([e.type for e in events], ["post.scheduled", "post.published"])
        self.assertEqual(events[0].subject, pid)
        self.assertEqual(events[0].attrs["platform"], "devto")
        self.assertEqual([e.seq for e in events], [1, 2])
        with self.assertRaisesRegex(ValueError, "unknown event type"):
            DomainEvent("post.exploded")

    def test_a_failed_sink_keeps_the_cursor_and_the_batch_is_replayed(self):
        for _ in range(3):
            self.post()
        flaky, memory = FlakySink(fail_times=1), MemorySink()
        poller = OutboxPoller(self.store, "insights", [flaky, memory], batch=2)
        with self.assertLogs("inkcast.libs.outbox", "ERROR") as logs:
            self.assertEqual(poller.run_once(), 0)                          # the first sink failed: nothing advanced
        self.assertIn("will retry", logs.output[0])
        self.assertEqual(self.store.cursor("insights"), 0)
        self.assertEqual(memory.events, [])                                  # later sinks were not run on that batch
        self.assertEqual(poller.drain(), 3)                                  # 2 + 1, then an empty poll
        self.assertEqual([e.seq for e in flaky.events], [1, 2, 3])
        self.assertEqual(len(memory.events), 3)
        self.assertEqual(self.store.cursor("insights"), 3)
        self.assertEqual(poller.run_once(), 0)                               # drained; nothing is handled twice
        self.assertEqual(len(memory.events), 3)

    def test_consumers_are_independent_and_lag_is_measured(self):
        for _ in range(4):
            self.post()
        OutboxPoller(self.store, "insights", [MemorySink()]).drain()
        OutboxPoller(self.store, "webhooks", [MemorySink()], batch=1).run_once()
        lag = self.store.outbox_lag()
        self.assertEqual(lag["insights"][0], 0)
        self.assertEqual(lag["webhooks"][0], 3)
        self.assertGreaterEqual(lag["webhooks"][1], 0.0)
        self.assertEqual(self.store.purge_outbox(keep_seconds=0), 1)         # only what every consumer passed
        self.assertEqual(len(self.store.poll("webhooks")), 3)

    def test_the_publish_pipeline_emits_an_event_per_outcome(self):
        import asyncio
        import httpx2
        from inkcast.libs.pipeline import Publisher
        from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult

        class Fake(Platform):
            id, name, default_api_url, credential_fields = "fake", "Fake", "https://fake.invalid", []
            mode = "ok"

            async def verify(self, creds):
                return Account(id="1", name="fake")

            def prepare(self, fields, body):
                return Prepared(platform="fake", title=fields["title"], payload={}, preview={})

            async def publish(self, creds, account, prepared, destination):
                if self.mode == "uncertain":
                    raise PlatformError("timeout", uncertain=True)
                if self.mode == "retry":
                    raise PlatformError("429", retryable=True)
                return PublishResult(url="https://fake/new", remote_id="new", status="published")

        fake = Fake(httpx2.AsyncClient())
        self.store.save_credentials("usr", "fake", {"k": "v"}, {"id": "1", "name": "fake"})
        publisher = Publisher(self.store, {"fake": fake})

        def due(title):
            return self.store.create_post(group_id="g", user_id="usr", platform="fake", title=title, status="scheduled",
                                          run_at=time.time() - 1, payload={"platform": "fake", "title": title, "payload": {},
                                                                           "preview": {}})
        ok, retry, uncertain = due("ok"), due("retry"), due("uncertain")
        fake.mode = "ok"
        asyncio.run(publisher.run_due())                                   # all three claimed; only "ok" mode here
        fake.mode = "retry"
        self.store.update_post(retry, status="scheduled", run_at=time.time() - 1)
        asyncio.run(publisher.run_due())
        fake.mode = "uncertain"
        self.store.update_post(uncertain, status="scheduled", run_at=time.time() - 1)
        asyncio.run(publisher.run_due())
        cancelled = self.post()
        publisher.cancel("usr", cancelled)
        kinds = [(e.type, e.subject, e.attrs.get("status") or e.attrs.get("retry")) for e in self.store.poll("t", 100)]
        self.assertIn(("post.published", ok, "published"), kinds)
        self.assertIn(("post.scheduled", retry, 2), kinds)                  # the retry re-queued it (attempt 2)
        self.assertIn(("post.unconfirmed", uncertain, None), kinds)
        self.assertIn(("post.cancelled", cancelled, None), kinds)
        self.assertEqual([k[0] for k in kinds][:3], ["post.scheduled"] * 3)


class Catalogue(unittest.TestCase):
    def test_every_event_type_is_catalogued_and_produced(self):
        used = set()
        for path in SRC.rglob("*.py"):
            text = path.read_text()
            used |= set(re.findall(r"""DomainEvent\(\s*["']([a-z]+\.[a-z_]+)["']""", text))
            used |= set(re.findall(r"""_event\(\s*["']([a-z]+\.[a-z_]+)["']""", text))
        self.assertEqual(used - set(EVENT_TYPES), set(), "event types emitted but not in libs/events.EVENT_TYPES")
        self.assertEqual(set(EVENT_TYPES) - used, set(), "event types catalogued but never emitted")


if __name__ == "__main__":
    unittest.main()
