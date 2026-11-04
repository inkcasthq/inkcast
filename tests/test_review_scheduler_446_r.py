# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of #451 (issue #446): one row's error must not stop the scheduler's pass for every later row.

#451 isolates each stuck row in ``run_due``'s recovery loop. The same pass's due loop still reads each post's credentials
in ``_send`` outside its guard, and issue #446 names that read: one person's credentials sealed under a previous
INKCAST_SECRET_KEY made ``Sealer.unseal`` raise out of ``run_due``, so every post claimed after theirs in the batch,
other people's included, stayed 'publishing' unsent. The next stuck pass then took them as interrupted sends: re-sent
after STUCK_AFTER on a platform that can be asked, and on one that can't (``Blind``, like Medium) marked 'unconfirmed'
for a person ("Inkcast restarted while this post was being sent"), though nothing was ever sent.

The post whose credentials can't be read stays claimed, for recovery, as it did when the tick stopped: a scheduler
started with the wrong key can read nobody's, and recovery sends them once the key is right
(``test_review_plugin_error_shapes``, #174)."""

import unittest

from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER

from test_person_decides import Blind, Harness


class DuePostsOfTwoPeople(Harness):
    platform_class = Blind

    def setUp(self):
        super().setUp()
        other = Store(self.path, "j" * 40)                                    # the key the credentials were sealed with
        self.addCleanup(other.close)
        other.save_credentials(self.user, self.platform.id, {"k": "v"}, {"id": "1", "name": "blind"})
        with self.assertRaises(RuntimeError):                                 # the premise: this store can't read them
            self.store.get_credentials(self.user, self.platform.id)
        self.colleague = self.store.upsert_user("local:colleague", "colleague", None, None)["id"]
        self.store.save_credentials(self.colleague, self.platform.id, {"k": "v"}, {"id": "2", "name": "colleague"})
        self.first = self.post("scheduled", "a")                              # claimed first: made first, due first
        self.second = self.post("scheduled", "b", user_id=self.colleague)


class FindingOneDuePostsUnreadableCredentialsDoNotStopTheBatch(DuePostsOfTwoPeople):
    def test_the_colleagues_post_is_sent_in_the_same_pass_and_the_unreadable_one_is_left_for_recovery(self):
        with self.assertLogs("inkcast.libs.pipeline", level="ERROR") as logged:
            outcomes = self.tick()                                            # raised RuntimeError on #451's head
        self.assertEqual((self.status(self.second), self.platform.sent), ("published", ["b"]),
                         "the colleague's post waited behind another person's credentials")
        self.assertEqual([o.post_id for o in outcomes], [self.second])
        self.assertEqual(self.status(self.first), "publishing", "the unsent post is not left claimed for recovery")
        self.assertEqual([getattr(r, "post_id", None) for r in logged.records], [self.first],
                         "the post that could not be sent is not named")
        self.tick(later=STUCK_AFTER + 1)                                      # its stuck pass: the colleague's is done
        self.assertEqual((self.status(self.second), self.platform.sent), ("published", ["b"]))


if __name__ == "__main__":
    unittest.main()
