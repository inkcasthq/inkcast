# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The ``stories:sync`` scope says it can cancel a removed file's pending post (#230).

Its description is the API reference's scope table (``libs/contracts/openapi.py``). A removal now cancels posts, a
power a person's own cancel guards with ``posts:write``; a token's owner reading the old text would not know it."""

import unittest

from inkcast.libs.auth import SCOPES


class TheSyncScopeSaysItCancels(unittest.TestCase):
    def test_the_description_names_the_cancel_of_a_removed_file(self):
        text = SCOPES["stories:sync"]
        self.assertIn("cancel", text)
        self.assertIn("removed", text)


if __name__ == "__main__":
    unittest.main()
