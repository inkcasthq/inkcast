# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #212: a team KPI names its member in ``kpi_rollups.dims``, not its scope.

``member_posts_published`` and ``member_sync_results`` are written at workspace and org scope with the member's user id
in ``dims.member`` (libs/kpi/rollup.py), which ``USER_TABLES``' condition for ``kpi_rollups`` (``scope_kind = 'user'``)
did not reach: an erasure left those rows naming the person. Now the condition reaches them, and ``erase_user`` folds
each into the row of the tombstone ``user:deleted`` of the same bucket before deleting it, so the workspace keeps its
totals (the organization's data: docs/design/observability-and-kpis.md §6) and nothing names the person; the owner
guard of issue #196 refuses such a row for a person who has no ``users`` row; the sweep takes the ones from before the
guard; a restore of a backup from before the erasure is folded again under the one audit row; and the statements
search the member index, never the table.

Every scenario drives the real engine (``RollupEngine`` over ``Store``) with ``post.published`` events in a workspace,
as E3 will emit them, and finds the rows that name a person by scanning ``dims`` for the id, never through the
condition under test.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import time
import unittest

from inkcast.libs.db import Store
from inkcast.libs.db.backup import restore_backup, take_backup
from inkcast.libs.events import DomainEvent
from inkcast.libs.kpi import RollupEngine, bucket_start
from test_erasure_orphans import orphan, rows_of, seed

KEY = "k" * 40
TOMBSTONE = "user:deleted"      # the design's tombstone id (§6); tests/test_architecture.py binds the constant to it
T = 1_700_000_000.0             # 2023-11-14T22:13:20Z
DAY = bucket_start(T, "day")


def published(user: str, workspace: str | None = None, platform: str = "devto") -> DomainEvent:
    """A post published by ``user``: in ``workspace`` (of org1), or on their own."""
    return DomainEvent("post.published", user_id=user, workspace_id=workspace, org_id="org1" if workspace else None,
                       subject="p", attrs={"platform": platform, "status": "published"}, at=T)


def naming(store: Store, user_id: str) -> list[tuple]:
    """Every KPI row that names the person anywhere: as its scope, or in the text of its dims."""
    return [tuple(r) for r in store._all("SELECT kpi, grain, scope_kind, scope_id, dims FROM kpi_rollups"
                                         " WHERE scope_id = ? OR instr(dims, ?) > 0", user_id, user_id)]


def members(store: Store, kpi: str, scope_kind: str, scope_id: str) -> dict[str, tuple[float, int]]:
    """``{member: (value, n)}`` over the day bucket of a team KPI at one scope, summed over the other dims."""
    out: dict[str, tuple[float, int]] = {}
    for p in store.read(kpi, "day", scope_kind, scope_id, DAY, DAY + 1):
        value, n = out.get(p.dims["member"], (0.0, 0))
        out[p.dims["member"]] = (value + p.value, n + p.n)
    return out


def total(store: Store, kpi: str, scope_kind: str, scope_id: str) -> float:
    return sum(p.value for p in store.read(kpi, "day", scope_kind, scope_id, DAY, DAY + 1))


class Workspace(unittest.TestCase):
    """Ada and Bob, members of workspace ws1 in org1: Ada published twice there and once on her own, Bob once there."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = str(self.dir / "inkcast.db")
        self.store = Store(self.path, KEY)
        self.addCleanup(self.store.close)
        self.ada, _ = seed(self.store, "ada")
        self.bob, _ = seed(self.store, "bob")
        RollupEngine(self.store).handle([published(self.ada, "ws1"), published(self.ada, "ws1", platform="medium"),
                                         published(self.ada), published(self.bob, "ws1")])
        self.assertEqual(members(self.store, "member_posts_published", "workspace", "ws1"),
                         {self.ada: (2.0, 2), self.bob: (1.0, 1)})
        self.assertEqual(total(self.store, "posts_published", "workspace", "ws1"), 3.0)
        self.bob_rows = rows_of(self.store, self.bob)

    def erase(self, user_id: str) -> dict[str, int]:
        return self.store.erase_user(user_id, as_read=self.store.get_user(user_id))

    def test_her_member_rows_fold_into_the_tombstone_and_the_workspace_keeps_its_totals(self):
        hers = rows_of(self.store, self.ada)["kpi_rollups"]
        at_her_scope = len(self.store._all("SELECT 1 FROM kpi_rollups WHERE scope_kind = 'user' AND scope_id = ?", self.ada))
        self.assertGreater(hers, at_her_scope, "the condition reaches the rows that name her in dims, not only at her scope")

        counts = self.erase(self.ada)
        self.assertEqual(naming(self.store, self.ada), [], "nothing in kpi_rollups names her: not as a scope, not in dims")
        self.assertEqual(counts["kpi_rollups"], hers, "every row that named her is in the count, the folded ones too")
        for scope_kind, scope_id in (("workspace", "ws1"), ("org", "org1")):
            self.assertEqual(members(self.store, "member_posts_published", scope_kind, scope_id),
                             {self.bob: (1.0, 1), TOMBSTONE: (2.0, 2)}, scope_kind)
            self.assertEqual(total(self.store, "posts_published", scope_kind, scope_id), 3.0, "the totals are untouched")
        self.assertEqual(total(self.store, "posts_published", "user", self.ada), 0.0, "her own rows are gone")
        self.assertEqual(total(self.store, "posts_published", "user", self.bob), 1.0)
        self.assertEqual(rows_of(self.store, self.bob), self.bob_rows, "bob is untouched")
        self.assertEqual(total(self.store, "posts_published", "platform", "devto"), 3.0, "application-wide rows are nobody's")
        # The tombstone's row keeps the other dims: one per platform, read back as any member's are.
        folded = self.store.read("member_posts_published", "day", "workspace", "ws1", DAY, DAY + 1, dims={"member": TOMBSTONE})
        self.assertEqual(sorted((p.dims["platform"], p.value, p.n) for p in folded), [("devto", 1.0, 1), ("medium", 1.0, 1)])
        (record,) = self.store.user_erasures()
        self.assertEqual(record["counts"], counts)
        # Erasing her again, by any path, changes nothing and adds no record.
        self.assertEqual(set(self.store.erase_user(self.ada).values()), {0})
        self.assertEqual(self.store.reapply_erasures(), {})
        self.assertEqual(len(self.store.user_erasures()), 1)

    def test_a_second_member_erased_later_folds_into_the_same_tombstone_row(self):
        self.erase(self.ada)
        self.erase(self.bob)
        self.assertEqual(naming(self.store, self.ada) + naming(self.store, self.bob), [])
        self.assertEqual(members(self.store, "member_posts_published", "workspace", "ws1"), {TOMBSTONE: (3.0, 3)})
        self.assertEqual(total(self.store, "posts_published", "workspace", "ws1"), 3.0)
        rows = self.store._all("SELECT dims FROM kpi_rollups WHERE kpi = 'member_posts_published' AND grain = 'day'"
                               " AND scope_kind = 'workspace'")
        self.assertEqual(len(rows), 2, "one tombstone row per platform, added up: not one per erased member")
        self.assertEqual(len(self.store.user_erasures()), 2)

    def test_a_late_event_for_an_erased_member_is_refused_where_it_is_written(self):
        """An event polled before the erasure and applied after it (issue #196): the rows that would name her are
        ignored by the guard, the workspace's own row lands, and no caller takes the refusal for a failure."""
        self.erase(self.ada)
        before = members(self.store, "member_posts_published", "workspace", "ws1")
        RollupEngine(self.store).handle([published(self.ada, "ws1")])
        self.assertEqual(naming(self.store, self.ada), [])
        self.assertEqual(members(self.store, "member_posts_published", "workspace", "ws1"), before,
                         "not folded either: a row for a person who has no users row is refused, not rewritten")
        self.assertEqual(total(self.store, "posts_published", "workspace", "ws1"), 4.0, "the workspace's own row is not hers")
        # An update that would move a row to her is ignored too (no repository makes one: defence in depth).
        moved = self.store._exec("UPDATE kpi_rollups SET dims = json_set(dims, '$.member', ?) WHERE scope_kind = 'workspace'"
                                 " AND json_extract(dims, '$.member') = ?", self.ada, self.bob)
        self.assertEqual(moved, 0)
        self.assertEqual(naming(self.store, self.ada), [])
        self.assertEqual(members(self.store, "member_posts_published", "workspace", "ws1"), before)

    def test_her_member_rows_from_before_the_guard_are_swept_in_housekeeping(self):
        orphan(self.store, self.ada)                     # her users row gone by hand; every row of hers left
        left = naming(self.store, self.ada)
        self.assertTrue(any(scope_kind == "workspace" for _, _, scope_kind, _, _ in left), left)
        swept = self.store.sweep_orphans()
        self.assertEqual(naming(self.store, self.ada), [])
        self.assertEqual(swept["kpi_rollups"], len(left), "the rows at her scope and the ones that name her in dims")
        self.assertEqual(rows_of(self.store, self.bob), self.bob_rows)
        self.assertEqual(members(self.store, "member_posts_published", "workspace", "ws1"), {self.bob: (1.0, 1)})

    def test_a_restore_of_a_backup_from_before_the_erasure_is_folded_again_under_the_one_audit_row(self):
        backups = self.dir / "backups"
        take_backup(pathlib.Path(self.path), backups, version="test")
        counts = self.erase(self.ada)
        audit = self.store.user_erasures()
        self.store.close()
        restore_backup(backups / sorted(p.name for p in backups.glob("*.db"))[-1], pathlib.Path(self.path),
                       now=time.time() + 3600)
        store = Store(self.path, KEY)
        self.addCleanup(store.close)
        self.assertEqual(members(store, "member_posts_published", "workspace", "ws1"),
                         {self.ada: (2.0, 2), self.bob: (1.0, 1)}, "the backup predates the erasure: her rows are back")
        self.assertEqual(store.reapply_erasures(), {self.ada: counts})
        self.assertEqual(naming(store, self.ada), [])
        self.assertEqual(members(store, "member_posts_published", "workspace", "ws1"),
                         {self.bob: (1.0, 1), TOMBSTONE: (2.0, 2)}, "folded once: the backup held no tombstone row")
        self.assertEqual(store.user_erasures(), audit, "no second audit row")

    def test_the_erasure_searches_the_member_index_and_never_scans_the_table(self):
        statements: list[str] = []
        self.store._conn.set_trace_callback(statements.append)
        self.erase(self.ada)
        self.store._conn.set_trace_callback(None)
        # The trace fires again, with the statement's own text, at the start of each trigger subprogram (the guard, once
        # per row the fold inserts), so the distinct statements are the ones that ran.
        touched = list(dict.fromkeys(s for s in statements if s.startswith(("INSERT INTO kpi_rollups", "DELETE FROM kpi_rollups"))))
        self.assertEqual([s[:6] for s in touched], ["INSERT", "DELETE"], "the fold, then the delete")
        for sql in touched:
            plan = " ".join(r[3] for r in self.store._conn.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertNotIn("SCAN kpi_rollups", plan, (sql, plan))
            self.assertIn("kpi_rollups_member", plan, (sql, plan))


if __name__ == "__main__":
    unittest.main()
