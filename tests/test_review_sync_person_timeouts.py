# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #146 (issue #121): a read about one person that gets no answer is that person's alone,
and only the second in a row fails the pass. Each test failed on the PR's head. #152 replaced the count in a row
with a budget per pass, and reads nobody twice in one pass: a person whose read got no answer in the admin-events
pass is not read again by the reconcile (``test_sync_timeout_budget.NotTwiceInAPass``), so each pass costs them one
read, and one person error, not two."""

import unittest

import httpx2
from test_sync_person_timeouts import T0, Scenario as _Scenario


class TheAdminEventsPassDoesNotCarryIntoTheReconcile(_Scenario):
    """The admin-events pass re-reads the minute before its cursor, and the cursor is the newest event's time, so in a
    quiet realm the person behind the newest USER admin event (the runbook's "fix the user there" makes one) is read
    at every pass. The PR's count of reads with no answer runs on from that pass into the reconcile, so when that
    person's read hangs and they are also the reconcile's first read, one person's two reads are "two reads in a
    row": the reconcile fails, nobody after them is cut off, and since it failed it is due again, and fails again, at
    every pass. Likewise two hanging people who are far apart in the reconcile's order fail it, where the runbook says
    only two next to each other do."""

    def edited(self, keycloak_id: str) -> None:
        self.kc.admin_events.append({"time": int(self.now * 1000) + 1_000, "resourceType": "USER",
                                     "operationType": "UPDATE", "resourcePath": f"users/{keycloak_id}"})

    def test_one_person_whose_read_hangs_read_by_both_passes_does_not_fail_the_reconcile(self):
        hangs, gone, disabled = self.people("ada", "bob", "carol")   # hangs sorts first: the reconcile's first read
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.kc.no_answer[hangs["kc"]] = httpx2.ReadTimeout
        self.kc.gone.add(gone["kc"])
        self.kc.disabled.add(disabled["kc"])
        self.edited(hangs["kc"])                                    # an administrator opened and saved them
        self.assertEqual(self.tick(worker, T0 + 30)["checked"], 1)
        self.assertEqual(self.tick(worker, T0 + 60)["checked"], 1, "the newest admin event is read at every pass")
        done = self.reconcile(worker, T0 + 90)
        self.assertEqual(self.pass_errors, [], "one person whose read hangs failed the whole reconcile (#121)")
        self.assertEqual((done["checked"], done["disabled"], done["erased"]), (3, 2, 0), "read once in the pass (#152)")
        self.assertEqual(self.kc.read[-3:], [gone["kc"], disabled["kc"], hangs["kc"]],
                         "known to hang: the admin-events pass leaves them to the reconcile, which reads them last")
        self.assert_cut_off(gone)
        self.assert_cut_off(disabled)
        self.assert_untouched(hangs)
        self.assertEqual(worker.last_reconcile, T0 + 90, "a reconcile that ran")
        self.assertEqual(self.errors_counted(), 3, "one person error per read: theirs alone, once per pass")

    def test_two_people_whose_reads_hang_far_apart_in_the_reconciles_order_do_not_fail_it(self):
        first, gone, edited, after = self.people("ada", "bob", "carol", "dave")   # by id: first and edited are apart
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.kc.no_answer.update({first["kc"]: httpx2.ReadTimeout, edited["kc"]: httpx2.ReadTimeout})
        self.kc.gone.add(gone["kc"])
        self.edited(edited["kc"])
        done = self.reconcile(worker, T0 + 30)
        self.assertEqual(self.pass_errors, [], "not next to each other in the reconcile's order (the runbook's rule)")
        self.assertEqual(self.kc.read, [edited["kc"], first["kc"], gone["kc"], after["kc"]], "the edited one not again (#152)")
        self.assertEqual((done["checked"], done["disabled"]), (4, 1))
        self.assert_cut_off(gone)
        for user in (first, edited, after):
            self.assert_untouched(user)
        self.assertEqual(self.errors_counted(), 2, "one person error per person per pass")


if __name__ == "__main__":
    unittest.main()
