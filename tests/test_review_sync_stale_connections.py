# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #195 (issue #182): a stale connection is read again, once, before it counts against a
person. Each test failed on the PR's head (34044ce).

The PR retries a ``ReadError`` or ``RemoteProtocolError`` only when it came back within ``STALE_WITHIN`` (1 s), so
that a connection cut after waiting (a proxy or firewall giving up on a lookup that hangs) is the hang itself,
counted at once: "a hang still costs a pass one timeout and one of the budget, never two". Keycloak ids carry non-hex
letters (``kc-ada``) so an Inkcast id (``usr_<hex>``) can't contain one by chance."""

import unittest

import httpx2
from test_sync_per_person_errors import T0
from test_sync_stale_connections import Stale


class HowLongAReadTookIsNotTheWallClocks(Stale):
    """The PR times the read on the sync's ``clock``, which is ``time.time`` in production: the wall clock, which NTP
    or a VM's clock sync steps. A proxy that cuts a lookup that hangs after 30 s, while the wall clock is stepped back
    by those 30 s, looks like a connection that failed at once: the read is made again, so that hang costs the pass two
    waits. How long a read took is the monotonic clock's to say."""

    STEP = 30.0                                  # the wall clock is stepped back by as much as the read waited

    def setUp(self):
        super().setUp()
        self.mono = 0.0

    def advance(self, seconds: float) -> None:
        """The failing read waited ``seconds`` (the monotonic clock), while the wall clock was stepped back."""
        self.mono += seconds
        self.now += seconds - self.STEP

    def test_a_connection_cut_after_waiting_is_not_read_again_when_the_wall_clock_steps_back(self):
        hangs, gone = self.people("kc-ada", "kc-bob")
        self.kc.stale[hangs["kc"]] = [httpx2.ReadError]            # it would answer at a second read: never made
        self.kc.after[hangs["kc"]] = 30.0                          # cut by a proxy after 30 s
        self.kc.gone.add(gone["kc"])
        worker = self.sync(30)
        worker.monotonic = lambda: self.mono
        done = self.reconcile(worker, T0)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(self.kc.read.count(hangs["kc"]), 1, "a hang that waited 30 s was read again: two waits")
        self.assertEqual((done["checked"], done["disabled"]), (2, 1))
        self.assert_untouched(hangs)
        self.assert_cut_off(gone)
        self.assertEqual(self.errors_counted(), 1, "the hang itself, counted at once")
        self.assertIn(hangs["kc"], worker._hanging)


if __name__ == "__main__":
    unittest.main()
