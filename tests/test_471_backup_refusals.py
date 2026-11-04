# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #471: follow-ups of the backup that refuses its retention (#405), from the OB3 review of PR #457.

1. A schedule slower than the refusal refuses every run. A run whose newest earlier backup is ``keep`` or more days
old (2 or more with keep 1) removes nothing and fails, and nothing tied the CronJob's ``schedule`` ("patch it per
environment") to ``INKCAST_BACKUP_KEEP``: a weekly schedule with keep 7 is refused every week, and no copy ever ages
out. The process cannot see its schedule (the pod has no service account token and no network), so the binding is a
manifest test: every overlay and environment that renders the CronJob runs it again before its keep refuses the run,
by the run's own rule (``_refusal``), on the run after the schedule's longest gap."""

import copy
import pathlib
import shutil
import unittest
from datetime import date, datetime, timedelta, timezone

from test_openfga_component import KUSTOMIZE, one, render

from inkcast.libs.config import backup_keep_from
from inkcast.libs.db.backup import _refusal

# A cron line's five fields and their bounds, as Kubernetes reads them (robfig/cron's standard parser): minute, hour,
# day of month, month, day of week (0 is Sunday).
FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
# 28 years, in which the days of the week and the leap days of 1901 to 2099 come round once, and a year more for the gap
# that spans the cycle's end.
FIRST, LAST = date(2001, 1, 1), date(2030, 1, 1)


def cron_field(text: str, low: int, high: int) -> tuple[set[int], bool]:
    """One field of a cron line: its values, and whether it is a star (``*`` or ``?``), which decides how the two day
    fields combine. Names (MON, JAN) raise, as a descriptor (@weekly) does in ``run_days``: the test fails rather than
    pass a schedule it did not read."""
    values, star = set(), False
    for item in text.split(","):
        span, _, step = item.partition("/")
        if span in ("*", "?"):
            start, end, star = low, high, star or step in ("", "1")
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
    ConfigMap gives it (as ``inkcast backup`` reads it): "" when that run may prune, as every run then may."""
    cronjob = one(docs, "CronJob", "inkcast-backup")
    keep = backup_keep_from(one(docs, "ConfigMap", "inkcast-config")["data"])
    schedule = cronjob["spec"]["schedule"]
    days = run_days(schedule)
    if len(days) < 2:
        return f"schedule {schedule!r} runs at most once from {FIRST} to {LAST}"
    gap, before, after = max(((b - a).days, a, b) for a, b in zip(days, days[1:]))
    at = {d: datetime(d.year, d.month, d.day, tzinfo=timezone.utc) for d in (before, after)}
    why = _refusal({pathlib.Path("before"): at[before]}, at[after].timestamp(), keep)
    return f"schedule {schedule!r} leaves {gap} days between two runs, INKCAST_BACKUP_KEEP is {keep}: {why}" if why else ""


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
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


if __name__ == "__main__":
    unittest.main()
