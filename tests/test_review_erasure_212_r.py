# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #393 (issue #212): a team KPI's member rows in the erasure.

``OWNERS`` now has two entries for ``kpi_rollups`` (its scope, and the member dim). ``orphan_condition`` and
``owner_guards`` take each table once, but ``sweep_orphans`` walked the entries, so it read ``kpi_rollups``, the
largest table, twice with the same condition: the second pass a full scan that can find nothing the first left.
"""

from __future__ import annotations

import collections
import unittest

from inkcast.libs.db import Store
from inkcast.libs.db.erasure import OWNERS

KEY = "k" * 40


class TheSweep(unittest.TestCase):
    def test_the_sweep_reads_each_table_once(self):
        store = Store(":memory:", KEY)
        self.addCleanup(store.close)
        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        self.assertEqual(store.sweep_orphans(), {})
        store._conn.set_trace_callback(None)
        reads = collections.Counter(s.split(" WHERE ")[0].removeprefix("SELECT rowid FROM ")
                                    for s in statements if s.startswith("SELECT rowid FROM "))
        self.assertEqual(dict(reads), {table: 1 for table in dict.fromkeys(owner[0] for owner in OWNERS)},
                         "one read per table: its condition already covers every way a row names a person")


if __name__ == "__main__":
    unittest.main()
