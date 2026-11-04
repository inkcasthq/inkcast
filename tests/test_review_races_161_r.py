# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The review of PR #397 (issue #161): what its tests do not drive.

The harness is ``tests/test_review_races_161.py``'s (``tests/test_erasure_recheck.py``'s ``Raced``: a second ``Store``
on the same database file for another process, the operator's ``inkcast identity`` command here)."""

import unittest

from inkcast.libs.db.identity import BREAKER_CONFIRMED, BREAKER_GONE
from test_review_races_161 import Base
from test_sync_breaker import T0


class TheReleasesCost(Base):
    """``release_breaker_people`` runs at every reconcile and for every person an admin event names, inside
    ``BEGIN IMMEDIATE``; a realm purge puts everyone in a pass. A statement that scans ``sync_breaker_people`` once
    per person is quadratic under the write lock: 20 000 held and read present held it for 3.5 s on the PR (42 ms on
    main, whose DELETE used the table's key)."""

    def plans(self, *args) -> list[str]:
        conn, statements = self.store._conn, []
        conn.set_trace_callback(lambda sql: statements.append(sql) if "sync_breaker_people" in sql else None)
        try:
            self.store.release_breaker_people(*args)
        finally:
            conn.set_trace_callback(None)
        self.assertTrue(statements, "it touched the table")
        return [row[3] for sql in statements for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]

    def test_no_statement_scans_the_table(self):
        ada, bob = self.person("kc-ada"), self.person("kc-bob")
        pass_id = self.store.record_breaker_trip(T0, 2, 10, [(ada["id"], ada["last_login_at"]), (bob["id"], bob["last_login_at"])])
        self.assertTrue(self.store.resolve_breaker_trip(pass_id, BREAKER_CONFIRMED, T0 + 1, "on-call"))
        for args in (([ada["id"], "usr_nobody"],), ([bob["id"]], BREAKER_GONE)):
            with self.subTest(args=args):
                plans = self.plans(*args)
                self.assertEqual([p for p in plans if p.startswith("SCAN sync_breaker_people")], [], plans)
        self.assertEqual(self.store.breaker_trip_people(pass_id), [], "both released, the pass claimed or not")


if __name__ == "__main__":
    unittest.main()
