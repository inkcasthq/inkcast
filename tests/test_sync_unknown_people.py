# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #181: the admin-events pass reads only people Inkcast knows.

Keycloak's admin events name everyone an administrator touched in the realm, and a realm is often shared with other
applications. The admin-events pass read each person named before it asked whether Inkcast knows them, so a
stranger's read that hangs (an LDAP referral to an unreachable server) used the pass's budget of reads with no
answer (#152): three strangers edited within the same minute ended, or failed, every pass before it reached the
people Inkcast does know, and the breaker, the budget and ``InkcastIdentitySyncPersonFailing`` all heard about
people who are nobody's. Now the pass asks the store first, and reads only a person with an Inkcast identity under
this Keycloak's prefix (``oidc:<issuer>#<id>``): a stranger costs no read, no budget, no error and no log line, as the
DELETE_ACCOUNT pass and the reconcile already did. Keycloak ids carry non-hex letters (``kc-ada``) so an Inkcast id
(``usr_<hex>``) can't contain one by chance."""

import unittest

import httpx2
from test_sync_per_person_errors import T0
from test_sync_timeout_budget import Budget


class TheAdminEventsPassReadsOnlyPeopleInkcastKnows(Budget):
    def test_three_strangers_whose_reads_hang_cost_no_budget_and_the_person_inkcast_knows_is_cut_off(self):
        """A realm shared with another application: three of its people, edited within the same minute, hang; the
        one person Inkcast knows was disabled a moment earlier, so their event is the oldest, read last. On main the
        strangers' three reads used the budget before them, and with no answer about anyone the pass failed."""
        (disabled,) = self.people("kc-ada")
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        strangers = ["kc-stranger-1", "kc-stranger-2", "kc-stranger-3"]    # no Inkcast identity: another application's
        for keycloak_id in strangers:
            self.kc.no_answer[keycloak_id] = httpx2.ReadTimeout
        self.kc.disabled.add(disabled["kc"])
        self.admin_event(disabled["kc"], 1_000)                            # the oldest event: read last
        for i, keycloak_id in enumerate(strangers):
            self.admin_event(keycloak_id, 2_000 + 1_000 * i)
        with self.assertLogs("inkcast.identity.sync", level="WARNING") as logs:
            done = self.tick(worker, T0 + 5)
        self.assertEqual(self.pass_errors, [], "the strangers' reads with no answer failed the pass")
        self.assertEqual(self.kc.read, [disabled["kc"]], "a stranger is not read at all")
        self.assertEqual((done["checked"], done["disabled"]), (1, 1), "checked counts the people Inkcast knows")
        self.assert_cut_off(disabled)
        self.assertEqual(self.errors_counted(), 0, "a stranger's hang is nobody's failure")
        self.assertEqual(self.failed(logs), [], "and no line names a person Inkcast does not have")
        self.assertEqual(self.ended(logs), [], "the budget was not touched")
        self.assertEqual([getattr(r, "event", "") for r in logs.records], ["identity.user.disabled"])
        self.assertEqual(worker._hanging, {}, "nor is a stranger known to hang: the reconcile never reads them")
        self.assertEqual(self.tick(worker, T0 + 35)["checked"], 1, "the look-back reads the known person again, them only")
        self.assertEqual(self.kc.read, [disabled["kc"], disabled["kc"]])
        self.assertEqual(self.pass_errors, [])

    def test_a_stranger_who_answers_is_not_read_either(self):
        """Not only the strangers who hang: an admin event about anyone without an Inkcast identity costs no read."""
        (known,) = self.people("kc-ada")
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["checked"], 0, "the cursors are set")
        self.admin_event("kc-stranger", 1_000)
        self.admin_event(known["kc"], 2_000)
        self.kc.gone.add("kc-stranger")                                    # deleted from the realm: still nobody's
        done = self.tick(worker, T0 + 5)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(self.kc.read, [known["kc"]])
        self.assertEqual((done["checked"], done["disabled"]), (1, 0))
        self.assert_untouched(known)

    def test_a_stranger_with_a_delete_account_event_is_not_read_as_before(self):
        """The guard the DELETE_ACCOUNT pass already had: a stranger who deleted their own account costs no read, and
        their event does not hold the cursor."""
        (known,) = self.people("kc-ada")
        worker = self.sync(30)
        self.assertEqual(self.tick(worker)["erased"], 0, "the cursors are set")
        self.kc.no_answer["kc-stranger"] = httpx2.ReadTimeout
        self.kc.events.append(self.event("kc-stranger", 1_000))
        done = self.tick(worker, T0 + 5)
        self.assertEqual(self.pass_errors, [])
        self.assertEqual(self.kc.read, [])
        self.assertEqual(done["erased"], 0)
        self.assertEqual(self.cursor(), self.kc.events[-1]["time"], "nothing held the cursor")
        self.assert_untouched(known)


if __name__ == "__main__":
    unittest.main()
