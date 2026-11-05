# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""#503's payload comparison holds only for a confirmed post: a scheduled one is not sent or replaced over it."""

import unittest

from test_sync_error_after_post import SyncCase, at


class PendingStatusGuard(SyncCase):
    def test_pending_post_is_not_replaced_by_payload_comparison(self):
        when = at(600)
        first = self.sync(when)
        self.assertEqual(first.status, "scheduled")
        self.assertTrue(self.store.update_post(first.post_id, payload="{}"))

        result = self.sync(when)
        self.assertEqual((result.action, result.post_id), ("unchanged", first.post_id))
        self.assertEqual(len(self.posts()), 1)


if __name__ == "__main__":
    unittest.main()
