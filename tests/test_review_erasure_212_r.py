# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #393 (issue #212): a team KPI's member rows in the erasure.

``OWNERS`` now has two entries for ``kpi_rollups`` (its scope, and the member dim). ``orphan_condition`` and
``owner_guards`` take each table once, but ``sweep_orphans`` walked the entries, so it read ``kpi_rollups``, the
largest table, twice with the same condition: the second pass a full scan that can find nothing the first left.

Two of the PR's guarantees had no test that a wrong fold or rule would fail: a member's count is added into an existing
tombstone row (``n + excluded.n``, not ``n + 1``), and every KPI a rule writes with a person dim is a sum (the rule's
``member`` flag adds the dim, not the catalogue's ``dims``, so a gauge folded by addition would pass). And the tombstone
names nobody to the guard and the sweep, so ``erase_user`` must not take it for a person: it would fold the tombstone's
rows into themselves and delete them, with every erased member's totals.
"""

from __future__ import annotations

import collections
import dataclasses
import unittest

from fakes import seed_people
from inkcast.libs.db import Store
from inkcast.libs.db.erasure import OWNERS
from inkcast.libs.events import DomainEvent
from inkcast.libs.kpi import BY_NAME, RULES, RollupEngine, bucket_start
from inkcast.libs.kpi.store import MEMBER_ERASED, PERSON_DIMS

KEY = "k" * 40
T = 1_700_000_000.0


def published(*members: str) -> list[DomainEvent]:
    return [DomainEvent("post.published", user_id=member, workspace_id="ws1", org_id="org1", subject="p",
                        attrs={"platform": "devto", "status": "published"}, at=T) for member in members]


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



class TheFold(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", KEY)
        self.addCleanup(self.store.close)
        seed_people(self.store, "usr_a", "usr_b", "usr_c")
        self.day = bucket_start(T, "day")

    def member_rows(self) -> list[tuple[str, float, int]]:
        return sorted((p.dims["member"], p.value, p.n)
                      for p in self.store.read("member_posts_published", "day", "workspace", "ws1", self.day, self.day + 1))

    def test_a_member_folded_into_an_existing_tombstone_row_adds_its_count(self):
        RollupEngine(self.store).handle(published("usr_a", "usr_a", "usr_b", "usr_b", "usr_b"))
        self.store.erase_user("usr_a")
        self.store.erase_user("usr_b")
        self.assertEqual(self.member_rows(), [(MEMBER_ERASED, 5.0, 5)])

    def test_the_tombstone_is_not_a_person_to_erase(self):
        RollupEngine(self.store).handle(published("usr_a", "usr_a", "usr_c"))
        self.store.erase_user("usr_a")
        self.assertEqual(set(self.store.erase_user(MEMBER_ERASED).values()), {0})
        self.assertEqual(self.member_rows(), [(MEMBER_ERASED, 2.0, 2), ("usr_c", 1.0, 1)])


class TheRules(unittest.TestCase):
    def test_every_kpi_a_rule_writes_with_a_person_dim_is_a_sum(self):
        for rule in RULES:
            event = DomainEvent(rule.event, user_id="usr_person", workspace_id="ws", org_id="org",
                                attrs={dim: f"{dim}-value" for dim in rule.dims})
            for inc in dataclasses.replace(rule, when=None, value=None).increments(event):
                if set(inc.dims) & set(PERSON_DIMS):
                    self.assertEqual(BY_NAME[inc.kpi].aggregation, "sum",
                                     f"{inc.kpi}: the erasure folds a member's rows by adding them")


if __name__ == "__main__":
    unittest.main()
