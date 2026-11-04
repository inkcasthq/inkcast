# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #112: every copy of the database ages out within ``INKCAST_BACKUP_KEEP`` days, as the deletion pages promise.

* Backups are removed by age, not by count (item 3): a copy dated ``keep`` or more days before the backup being taken
  goes, after that backup is good, however long the CronJob failed in between; an extra backup on the same day evicts
  no daily recovery point. The alert on a missed backup, ``InkcastBackupMissing``, is what covers the days in between.
* The copy a restore leaves beside the database (``inkcast.db.before-restore-<time>``, item 1) ages out by the same
  rule, with its WAL files, at the next good backup; a file whose name carries no time is never touched. The runbook
  says so.
* The CronJob reads ``INKCAST_BACKUP_KEEP`` from the ``inkcast-config`` ConfigMap the server reads (item 2): one
  source, in every overlay and environment that renders the backup component.
* The runbook says that an off-cluster copy (a volume snapshot, Velero) keeps the promise only with a retention of at
  most as many days (item 4)."""

import logging
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

import yaml

from inkcast.libs.alerting import ALERTS
from inkcast.libs.db import Store
from inkcast.libs.db.backup import BackupError, aside_copies, list_backups, restore_backup, take_backup

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "runbooks" / "backup-restore.md"
KEY = "k" * 40
DAY = 86400
# The CronJob's hour, 03:17 UTC, on a fixed day: every stamp below is a known date.
DAY0 = datetime(2026, 11, 1, 3, 17, tzinfo=timezone.utc).timestamp()


def dates(paths: list[pathlib.Path]) -> list[str]:
    return [p.name.split("-", 1)[1][:8] for p in paths]


class Retention(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "data" / "inkcast.db"
        self.db.parent.mkdir()
        self.store = Store(str(self.db), KEY)
        self.addCleanup(self.store.close)
        self.store.upsert_user("local:owner", "owner", None, None)
        self.backups = self.dir / "backups"

    def test_backups_age_out_by_days_not_by_count(self):
        removed = [take_backup(self.db, self.backups, keep=2, now=DAY0 + i * DAY).removed_backups for i in range(5)]
        self.assertEqual(removed, [0, 0, 1, 1, 1], "from the third day on, each backup removes the one two days older")
        self.assertEqual(dates(list_backups(self.backups)), ["20261104", "20261105"])
        # Two more on the same day (one taken by hand before a risky change) evict no daily recovery point.
        for hour in (1, 2):
            take_backup(self.db, self.backups, keep=2, now=DAY0 + 4 * DAY + hour * 3600)
        self.assertEqual(dates(list_backups(self.backups)), ["20261104", "20261105", "20261105", "20261105"])
        # The CronJob fails for two weeks: the first good backup cannot tell that gap from a clock that jumped ahead,
        # so it removes nothing and says so (#405); the next removes everything older than keep days at once.
        b = take_backup(self.db, self.backups, keep=2, now=DAY0 + 21 * DAY)
        self.assertEqual(b.removed_backups, 0)
        self.assertIn("17 days old", b.refused)
        b = take_backup(self.db, self.backups, keep=2, now=DAY0 + 22 * DAY)
        self.assertEqual((b.removed_backups, b.refused), (4, ""))
        self.assertEqual(dates(list_backups(self.backups)), ["20261122", "20261123"])

    def test_keep_1_leaves_the_day_s_copies_and_the_newest_is_never_removed(self):
        take_backup(self.db, self.backups, keep=1, now=DAY0)
        take_backup(self.db, self.backups, keep=1, now=DAY0 + DAY)
        self.assertEqual(dates(list_backups(self.backups)), ["20261102"])
        # A clock far ahead of the copies (keep=1, a copy from a year ago) keeps the one just taken, and the earlier one
        # too: a run that would leave only its own copy removes nothing (#405).
        b = take_backup(self.db, self.backups, keep=1, now=DAY0 + 400 * DAY)
        self.assertEqual([p.name for p in list_backups(self.backups, now=DAY0 + 400 * DAY)][-1], b.name)
        self.assertEqual((dates(list_backups(self.backups)), b.removed_backups), (["20261102", "20271206"], 0))
        self.assertIn("399 days old", b.refused)

    def test_a_failed_backup_removes_nothing(self):
        take_backup(self.db, self.backups, keep=1, now=DAY0)
        with self.assertRaises(BackupError):
            take_backup(self.dir / "missing.db", self.backups, keep=1, now=DAY0 + 9 * DAY)
        self.assertEqual(dates(list_backups(self.backups)), ["20261101"])

    def test_a_copy_whose_name_carries_no_day_is_left_alone(self):
        odd = self.backups / "inkcast-by-hand.db"
        self.backups.mkdir()
        odd.write_bytes(b"")
        odd.with_suffix(".json").write_text("{}")
        take_backup(self.db, self.backups, keep=1, now=DAY0 + 400 * DAY)
        self.assertTrue(odd.exists())

    def test_a_restore_s_copy_of_the_replaced_database_ages_out_with_the_backups(self):
        b = take_backup(self.db, self.backups, keep=2, now=DAY0)
        self.store.close()
        aside = restore_backup(b.path, self.db, now=DAY0 + 3600)
        self.assertEqual(aside_copies(self.db), [aside])
        for extra in ("-wal", "-shm"):                      # the WAL files a restore moves aside with a file left mid-write
            aside.with_name(aside.name + extra).write_bytes(b"")
        by_hand = self.db.with_name("inkcast.db.before-restore-by-hand")   # no time in the name: never touched
        by_hand.write_bytes(b"")
        kept = take_backup(self.db, self.backups, keep=2, now=DAY0 + DAY)
        self.assertEqual((kept.removed_backups, kept.removed_copies), (0, 0))
        self.assertTrue(aside.exists(), "a day old: within keep days")
        gone = take_backup(self.db, self.backups, keep=2, now=DAY0 + 2 * DAY)
        self.assertEqual((gone.removed_backups, gone.removed_copies), (1, 1))
        self.assertFalse(aside.exists())
        for extra in ("-wal", "-shm"):
            self.assertFalse(aside.with_name(aside.name + extra).exists(), f"the copy's {extra} file went with it")
        self.assertTrue(by_hand.exists())
        self.assertEqual(aside_copies(self.db), [])
        restored = sqlite3.connect(self.db)
        self.addCleanup(restored.close)
        self.assertEqual(restored.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1, "the database itself is untouched")

    def test_the_command_logs_what_it_removed(self):
        import types
        from unittest import mock
        from inkcast.backup import backup_main
        from inkcast.libs.db import backup as module
        take_backup(self.db, self.backups, keep=1, now=DAY0 - DAY)
        self.store.close()
        restore_backup(list_backups(self.backups)[0], self.db, now=DAY0 - DAY + 60)
        # The command on the day after: its clock injected, so the day since the copies is one whatever today is (#405).
        with mock.patch.object(module, "time", types.SimpleNamespace(time=lambda: DAY0)), \
                self.assertLogs("inkcast.backup", logging.INFO) as logs:
            self.assertEqual(backup_main(["--database", str(self.db), "--to", str(self.backups), "--keep", "1"]), 0)
        (done,) = [r for r in logs.records if getattr(r, "event", "") == "backup.done"]
        self.assertEqual((done.removed_backups, done.removed_copies), (1, 1))
        self.assertIn("removed 1 backup(s) and 1 pre-restore cop(ies) 1 or more days old", done.getMessage())


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
class TheCronJob(unittest.TestCase):
    """One source for the number of days: the CronJob reads ``INKCAST_BACKUP_KEEP`` from the ConfigMap the server
    reads, so the deletion pages and the retention cannot disagree."""

    TARGETS = ("overlays/ci", "overlays/kubernetes", "overlays/openshift", "environments/staging",
               "environments/production")

    @staticmethod
    def render(path: str) -> list[dict]:
        out = subprocess.run(["kubectl", "kustomize", str(ROOT / "deploy" / "kustomize" / path)],
                             capture_output=True, text=True, check=True)
        return [d for d in yaml.safe_load_all(out.stdout) if d]

    def test_the_cronjob_reads_keep_from_the_configmap_the_server_reads(self):
        for target in self.TARGETS:
            with self.subTest(target=target):
                docs = self.render(target)
                (cronjob,) = [d for d in docs if d["kind"] == "CronJob" and d["metadata"]["name"] == "inkcast-backup"]
                (container,) = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
                self.assertNotIn("INKCAST_BACKUP_KEEP", [e["name"] for e in container.get("env", [])],
                                 "the number is not written into the CronJob")
                self.assertEqual(container.get("envFrom"), [{"configMapRef": {"name": "inkcast-config"}}])
                config = [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "inkcast-config"]
                self.assertEqual(len(config), 1, "the ConfigMap the CronJob names is rendered beside it")
                servers = [d for d in docs if d["kind"] == "Deployment"
                           and any(ref.get("configMapRef", {}).get("name") == "inkcast-config"
                                   for c in d["spec"]["template"]["spec"]["containers"] for ref in c.get("envFrom", []))]
                self.assertTrue(servers, "the server reads the same ConfigMap")
                if target.startswith("environments/"):
                    self.assertIn("INKCAST_BACKUP_KEEP", config[0]["data"], "an environment states the number once")


class TheAlertAndTheRunbook(unittest.TestCase):
    def test_a_missed_day_of_backups_is_a_critical_alert(self):
        """Item 3: retention by age holds from the next good backup; the alert covers the days without one."""
        (alert,) = [a for a in ALERTS if a.name == "InkcastBackupMissing"]
        self.assertEqual(alert.severity, "critical")
        self.assertIn('kube_cronjob_status_last_successful_time{cronjob="inkcast-backup"} > 26 * 3600', alert.expr)
        self.assertIn("InkcastBackupMissing", RUNBOOK.read_text())

    def test_the_runbook_says_the_restore_s_copy_ages_out(self):
        (line,) = [line for line in RUNBOOK.read_text().splitlines() if "inkcast.db.before-restore-<time>" in line]
        self.assertIn("INKCAST_BACKUP_KEEP", line, "the copy a restore leaves ages out within the same days")

    def test_the_runbook_bounds_the_retention_of_off_cluster_copies(self):
        off_cluster = RUNBOOK.read_text().split("## Off-cluster copies", 1)[1].split("\n## ", 1)[0]
        self.assertIn("at most `INKCAST_BACKUP_KEEP` days", off_cluster)
        for word in ("snapshot", "Velero", "inkcast-data"):
            self.assertIn(word, off_cluster)


if __name__ == "__main__":
    unittest.main()
