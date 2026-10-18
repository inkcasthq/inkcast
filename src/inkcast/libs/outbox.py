# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The outbox poller: reads domain events in order and hands each batch to the ``EventSink``s of one
named consumer, then advances that consumer's cursor.

At-least-once: if a sink raises, the cursor stays, the failure is logged (``outbox.consumer.failed``)
and counted, and the next tick replays the batch. Sinks are idempotent by event id, so a replay is
harmless. One consumer per purpose (``insights``, later ``webhooks``), each with its own cursor, so a
slow sink never holds another back. A broker later is just another sink (publish the batch, ack).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from .db.outbox import OutboxRepo
from .events import EventSink

log = logging.getLogger(__name__)


class OutboxPoller:
    def __init__(self, store: OutboxRepo, consumer: str, sinks: list[EventSink], batch: int = 200,
                 metrics: Any = None):
        self.store, self.consumer, self.sinks, self.batch, self.metrics = store, consumer, list(sinks), batch, metrics
        self.last_tick: float = 0.0

    def run_once(self) -> int:
        """Handle one batch; the number of events handled (0 when the outbox is drained)."""
        events = self.store.poll(self.consumer, self.batch)
        if not events:
            self.store.advance(self.consumer, self.store.cursor(self.consumer))  # keep the cursor row fresh
            return 0
        for sink in self.sinks:
            try:
                sink.handle(events)
                self._count(sink.name, "handled", len(events))
            except Exception:
                self._count(sink.name, "failed", len(events))
                log.exception("Outbox consumer %s: sink %s failed on %d event(s) from seq %s; will retry",
                              self.consumer, sink.name, len(events), events[0].seq,
                              extra={"event": "outbox.consumer.failed", "consumer": self.consumer, "sink": sink.name})
                return 0
        self.store.advance(self.consumer, events[-1].seq or 0)
        return len(events)

    def drain(self) -> int:
        total = 0
        while True:
            n = self.run_once()
            total += n
            if n < self.batch:
                return total

    async def run_forever(self, interval: float, stop: asyncio.Event, on_tick: Callable[[], None] | None = None) -> None:
        log.info("Outbox consumer %s running every %.0fs (%s)", self.consumer, interval, ", ".join(s.name for s in self.sinks))
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.drain)
                self.last_tick = time.time()
                if on_tick:
                    on_tick()
            except Exception:
                log.exception("Outbox consumer %s tick failed", self.consumer,
                              extra={"event": "outbox.consumer.failed", "consumer": self.consumer, "sink": "-"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        log.info("Outbox consumer %s stopped", self.consumer)

    def _count(self, sink: str, result: str, n: int) -> None:
        if self.metrics is not None:
            self.metrics.inc("inkcast_outbox_events_total", n, sink=sink, result=result)


__all__ = ["OutboxPoller"]
