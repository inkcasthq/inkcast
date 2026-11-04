# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #471: follow-ups of the backup that refuses its retention (#405), from the OB3 review of PR #457.

1. A schedule slower than the refusal refuses every run. A run whose newest earlier backup is ``keep`` or more days
old (2 or more with keep 1) removes nothing and fails, and nothing tied the CronJob's ``schedule`` ("patch it per
environment") to ``INKCAST_BACKUP_KEEP``: a weekly schedule with keep 7 is refused every week, and no copy ever ages
out. The process cannot see its schedule (the pod has no service account token and no network), so the binding is a
manifest test: every overlay and environment that renders the CronJob runs it again before its keep refuses the run,
by the run's own rule (``_refusal``), on the run after the schedule's longest gap.

2. A refused day costs one copy, not three. Each refused attempt kept a whole copy, and the Job's ``backoffLimit: 2``
gives three attempts, on a volume sized for one copy a day. Now an attempt that finds today's copy of the same database
there already, on a day whose retention is refused, takes none and is refused the same way: the refusal rests on the
days before today, so every copy of today was taken by a run refused alike. The first attempt is as before.

3. A manifest nested too deep crashed every reader of the backups. Parsing it raises RecursionError, which nothing
caught: ``take_backup`` raised before it copied anything (no ``backup.failed`` line, a traceback), and so did
``list_backups`` (``restore --latest``, ``inkcast backups``). Now a manifest that cannot be read, of any shape, counts as
having no ``previous`` (the copy is dated by its name) and is listed; ``verify_backup`` calls it unreadable, so
``inkcast backups`` shows the copy as broken and ``restore --latest`` refuses it."""

import contextlib
import copy
import io
import logging
import pathlib
import shutil
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from test_backup_clock import DAY, DAY0, JUMP, KEEP, FiveDailyBackups, names, stamp
from test_openfga_component import KUSTOMIZE, one, render

from inkcast.backup import backup_main, list_main, restore_main
from inkcast.libs.config import backup_keep_from
from inkcast.libs.db import backup as module
from inkcast.libs.db.backup import ASIDE, _refusal, list_backups, take_backup

# A cron line's five fields and their bounds, as Kubernetes reads them (robfig/cron's standard parser): minute, hour,
# day of month, month, day of week (0 is Sunday).
FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
# 28 years, in which the days of the week and the leap days of 1901 to 2099 come round once, and a year more for the gap
# that spans the cycle's end.
FIRST, LAST = date(2001, 1, 1), date(2030, 1, 1)
DEPTH = 1_000_000           # parsing raises RecursionError: Python 3.14 here parses 100 000 levels, not 200 000


def cron_field(text: str, low: int, high: int) -> tuple[set[int], bool]:
    """One field of a cron line: its values, and whether it is a star (``*`` or ``?``), which decides how the two day
    fields combine. Names (MON, JAN) raise, as a descriptor (@weekly) does in ``run_days``: the test fails rather than
    pass a schedule it did not read."""
    values, star = set(), False
    for item in text.split(","):
        span, _, step = item.partition("/")
        if span in ("*", "?"):
            start, end, star = low, high, star or int(step or 1) == 1      # robfig: a star unless the step is above 1
        else:
            first, _, last = span.partition("-")
            start = int(first)
            end = int(last) if last else high if step else start
        if not low <= start <= end <= high:
            raise ValueError(f"{text!r}: outside {low}-{high}")
        values.update(range(start, end + 1, int(step or 1)))
    return values, star


def run_days(schedule: str) -> list[date]:
    """The days from FIRST to LAST on which a CronJob with ``schedule`` runs at least once. A day of month and a day of
    week both restricted match either, as in cron; with either a star, both must."""
    fields = schedule.split()
    if len(fields) != len(FIELDS):
        raise ValueError(f"{schedule!r}: not five fields")
    _, _, (mdays, mstar), (months, _), (wdays, wstar) = [cron_field(f, *bounds) for f, bounds in zip(fields, FIELDS)]

    def runs(d: date) -> bool:
        in_month, in_week = d.day in mdays, d.isoweekday() % 7 in wdays
        return d.month in months and (in_month and in_week if mstar or wstar else in_month or in_week)

    return [d for d in (FIRST + timedelta(n) for n in range((LAST - FIRST).days)) if runs(d)]


def refusal(docs: list[dict]) -> str:
    """What retention says on the run after the longest gap of the backup CronJob in ``docs``, with the keep its
    ConfigMap gives it (as ``inkcast backup`` reads it): "" when that run may prune, as every run then may. A keep the
    CronJob sets itself, ``--keep`` in its args (or argparse's abbreviation of it) or INKCAST_BACKUP_KEEP in its env,
    comes before the ConfigMap's, so it fails: the one value is the ConfigMap's (#112)."""
    cronjob = one(docs, "CronJob", "inkcast-backup")
    (container,) = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    own = [a for a in container.get("args", []) if a.startswith("--k")] + \
          [e["name"] for e in container.get("env", []) if e["name"] == "INKCAST_BACKUP_KEEP"]
    if own:
        return f"the CronJob sets the keep its runs use itself ({', '.join(own)}), not through the ConfigMap"
    keep = backup_keep_from(one(docs, "ConfigMap", "inkcast-config")["data"])
    schedule = cronjob["spec"]["schedule"]
    days = run_days(schedule)
    if len(days) < 2:
        return f"schedule {schedule!r} runs at most once from {FIRST} to {LAST}"
    gap, before, after = max(((b - a).days, a, b) for a, b in zip(days, days[1:]))
    at = {d: datetime(d.year, d.month, d.day, tzinfo=timezone.utc) for d in (before, after)}
    why = _refusal({pathlib.Path("before"): at[before]}, at[after].timestamp(), keep)
    return f"schedule {schedule!r} leaves {gap} days between two runs, INKCAST_BACKUP_KEEP is {keep}: {why}" if why else ""


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's test job has the runner image's)")
class TheScheduleAgainstKeep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rendered = {f"{d.parent.name}/{d.name}": render(f"{d.parent.name}/{d.name}")
                    for d in sorted([*KUSTOMIZE.glob("overlays/*"), *KUSTOMIZE.glob("environments/*")])}
        cls.docs = {target: docs for target, docs in rendered.items()
                    if any(d["kind"] == "CronJob" and d["metadata"]["name"] == "inkcast-backup" for d in docs)}

    def test_every_rendered_backup_cronjob_runs_again_before_its_keep_refuses_the_run(self):
        self.assertLessEqual({"overlays/ci", "overlays/kubernetes", "overlays/openshift", "environments/staging",
                              "environments/production"}, set(self.docs))
        for target, docs in self.docs.items():
            with self.subTest(target=target):
                # The days the schedule names are the UTC days retention counts.
                self.assertEqual(one(docs, "CronJob", "inkcast-backup")["spec"]["timeZone"], "Etc/UTC")
                why = refusal(docs)
                if why:
                    self.fail(why)

    def test_a_slower_schedule_with_the_same_keep_is_refused(self):
        """Staging's render with slower schedules. Its keep is stated here, so the boundaries hold whatever staging's
        value: days 1, 15 and 29 leave 14 days between two runs (1 to 15, 15 February to 1 March), days 1, 14 and 27
        leave 13, and the odd days of a month leave 2, which keep 1 refuses (more than one day)."""
        docs = copy.deepcopy(self.docs["environments/staging"])
        data, spec = one(docs, "ConfigMap", "inkcast-config")["data"], one(docs, "CronJob", "inkcast-backup")["spec"]
        for keep, schedule, refused in (("14", "17 3 */14 * *", True), ("14", "17 3 1 * *", True),
                                        ("14", "17 3 1,15 * *", True), ("14", "17 3 */13 * *", False),
                                        ("14", "17 3 * * 0", False), ("1", "17 3 */2 * *", True),
                                        ("1", "17 3 * * *", False), ("1", "0 */6 * * *", False)):
            with self.subTest(keep=keep, schedule=schedule):
                data["INKCAST_BACKUP_KEEP"], spec["schedule"] = keep, schedule
                self.assertEqual(bool(refusal(docs)), refused, refusal(docs))
        data["INKCAST_BACKUP_KEEP"], spec["schedule"] = "14", "17 3 */14 * *"
        self.assertIn("leaves 14 days between two runs, INKCAST_BACKUP_KEEP is 14: the newest earlier backup",
                      refusal(docs))


class ARefusedDay(FiveDailyBackups):
    """Five daily backups, then a day whose retention is refused: a clock a year ahead, or a gap of 15 days."""

    def attempts(self, now: float) -> list:
        return [take_backup(self.db, self.backups, keep=KEEP, now=now + i * 60) for i in range(3)]

    def test_three_attempts_on_a_refused_day_leave_one_copy(self):
        first, *retries = self.attempts(JUMP)
        self.assertEqual(names(list_backups(self.backups, now=JUMP)), names(d.path for d in self.days) + [first.name])
        self.assertEqual([r.name for r in retries], [first.name] * 2, "each retry names the copy that holds the day")
        self.assertEqual([b.copied for b in (first, *retries)], [True, False, False])
        self.assertTrue(first.refused.endswith("so nothing was removed; the next good backup removes them."),
                        "the first attempt's refusal as before")
        for r in retries:
            self.assertEqual(r.refused, f"{first.refused} Today's copy, {first.name}, is there already, so this run took none.")
            self.assertEqual((r.removed_backups, r.removed_copies, r.manifest), (0, 0, first.manifest))
        self.assertTrue(all(d.path.exists() for d in self.days))

    def test_after_a_gap_the_retries_take_none_and_the_next_day_prunes_as_before(self):
        first, *_ = self.attempts(DAY0 + 19 * DAY)                                  # 15 days after the last one
        self.assertEqual(len(list_backups(self.backups, now=DAY0 + 19 * DAY)), 6)
        c = take_backup(self.db, self.backups, keep=KEEP, now=DAY0 + 20 * DAY)    # a day on: a gap of one day
        self.assertEqual((c.removed_backups, c.refused, c.copied), (5, "", True))
        self.assertEqual(names(list_backups(self.backups, now=DAY0 + 20 * DAY)), [first.name, c.name])

    def test_the_job_s_three_attempts_each_fail_and_only_the_first_logs_backup_done(self):
        events, codes = [], []
        for i in range(3):
            with mock.patch.object(module, "time", types.SimpleNamespace(time=lambda i=i: JUMP + i * 60)), \
                    mock.patch("inkcast.backup._logging"), self.assertLogs("inkcast.backup", logging.INFO) as logs:
                codes.append(backup_main(["--database", str(self.db), "--to", str(self.backups), "--keep", str(KEEP)]))
            events.append([getattr(r, "event", "") for r in logs.records])
            refused = logs.records[-1]
            self.assertEqual((refused.levelno, refused.backup), (logging.ERROR, f"inkcast-{stamp(JUMP)}.db"))
        self.assertEqual(codes, [1, 1, 1], "every attempt fails, so the Job does and InkcastBackupMissing fires")
        self.assertEqual(events, [["backup.done", "backup.retention.refused"]] + [["backup.retention.refused"]] * 2)
        self.assertEqual(len(list_backups(self.backups, now=JUMP)), 6)

    def test_a_backup_of_a_restore_s_copy_on_a_refused_day_is_taken_and_counts_for_no_retry(self):
        # The runbook's way back to the database a restore replaced: a backup of that copy, named by its time.
        first, *_ = self.attempts(JUMP)
        aside = self.db.with_name(f"{self.db.name}{ASIDE}{stamp(JUMP - 3600)}")
        aside.write_bytes(self.db.read_bytes())
        copy_of_aside = take_backup(aside, self.backups, keep=KEEP, now=JUMP + 600)
        self.assertEqual((copy_of_aside.copied, copy_of_aside.name), (True, f"inkcast-{stamp(JUMP - 3600)}.db"))
        retry = take_backup(self.db, self.backups, keep=KEEP, now=JUMP + 900)
        self.assertEqual((retry.copied, retry.name), (False, first.name))
        self.assertEqual(len(list_backups(self.backups, now=JUMP)), 7)


class AManifestNestedTooDeep(FiveDailyBackups):
    """A copy a clock a year ahead took, whose day ``_dated`` reads from its manifest's ``previous`` once the clock is
    right again, and whose manifest is then rewritten nested too deep to parse (it takes write access to the volume)."""

    def setUp(self):
        super().setUp()
        self.future = take_backup(self.db, self.backups, keep=KEEP, now=JUMP)
        self.future.path.with_suffix(".json").write_text("[" * DEPTH + "]" * DEPTH)
        self.now = DAY0 + 5 * DAY

    def test_the_backup_is_taken_and_list_backups_lists_it(self):
        b = take_backup(self.db, self.backups, keep=KEEP, now=self.now)
        self.assertEqual((b.copied, b.refused, b.removed_backups), (True, "", 0))
        # No ``previous``: dated by its name, a year on, as a manifest from before #405 is (tests/test_backup_clock.py).
        self.assertEqual(names(list_backups(self.backups, now=self.now)),
                         names(d.path for d in self.days) + [b.name, self.future.name])

    def test_a_manifest_of_any_shape_that_cannot_be_read_counts_as_no_previous(self):
        nested = "[" * DEPTH + "]" * DEPTH
        shapes = {"nested object": '{"a":' * DEPTH + "1" + "}" * DEPTH, "nested previous": f'{{"previous": {nested}}}',
                  "a list": "[1, 2]", "a string": '"20261105T031700Z"', "not JSON": "{", "not UTF-8": b"\xff\xfe"}
        for i, (shape, text) in enumerate(shapes.items(), start=1):
            with self.subTest(shape=shape):
                self.future.path.with_suffix(".json").write_bytes(text if isinstance(text, bytes) else text.encode())
                b = take_backup(self.db, self.backups, keep=KEEP, now=self.now + i * 60)
                self.assertEqual((b.copied, b.refused), (True, ""))
                self.assertEqual(list_backups(self.backups, now=self.now + i * 60)[-2:], [b.path, self.future.path])

    def test_the_commands_back_up_list_and_refuse_to_restore_it_without_a_traceback(self):
        with mock.patch.object(module, "time", types.SimpleNamespace(time=lambda: self.now)), \
                mock.patch("inkcast.backup._logging"), self.assertLogs("inkcast.backup", logging.INFO) as logs:
            self.assertEqual(backup_main(["--database", str(self.db), "--to", str(self.backups), "--keep", str(KEEP)]), 0)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(list_main(["--to", str(self.backups)]), 0)
            self.assertEqual(restore_main(["--latest", str(self.backups), "--database", str(self.db)]), 1)
        self.assertEqual([getattr(r, "event", "") for r in logs.records], ["backup.done", "restore.refused"])
        self.assertIn("manifest is empty or can't be read", logs.records[-1].getMessage())
        lines = out.getvalue().splitlines()
        self.assertEqual([line.split()[0] for line in lines], names(d.path for d in self.days) +
                         [f"inkcast-{stamp(self.now)}.db", self.future.name])
        self.assertTrue(all(line.endswith(" ok") for line in lines[:-1]), lines)
        self.assertIn("BROKEN", lines[-1])


if __name__ == "__main__":
    unittest.main()
