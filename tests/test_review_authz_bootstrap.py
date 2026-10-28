# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #317 (E3.F3.9.S6, issue #315): defects found in the start-up pinning by store name."""

from __future__ import annotations

import unittest

from test_authz_bootstrap import BASE, FakeOpenFga

from inkcast.libs.config import ConfigError, load_config

STORES = ("01JFAKE0000000000000000001", "01JFAKE0000000000000000002")


class DuplicateStoreRemedy(FakeOpenFga):
    """Two stores of the name: the refusal a pod prints must name a remedy the pod then accepts."""

    def test_the_remedy_for_two_stores_is_not_half_a_pin(self):
        self.fakes.fga_stores = {sid: "inkcast" for sid in STORES}
        self.fakes.fga_models = {sid: [] for sid in STORES}
        message = self.refusal(self.config())
        self.assertIn("2 stores named 'inkcast'", message)
        # The store id alone, as the refusal said on the PR head, is half a pin: Config.validate refuses it.
        half = {**BASE, "INKCAST_FGA_API_URL": "http://openfga:8080", "INKCAST_FGA_STORE_NAME": "inkcast",
                "INKCAST_FGA_STORE_ID": STORES[0]}
        with self.assertRaises(ConfigError) as caught:
            load_config(half).validate(("secret_key", "login", "authz"))
        self.assertIn("needs INKCAST_FGA_MODEL_ID", str(caught.exception))
        # So a remedy that pins by hand names both ids, and the one that keeps pinning by name (and lets the Job
        # run again) is to delete or rename the other stores.
        self.assertIn("INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID", message)
        self.assertIn("delete or rename", message)


if __name__ == "__main__":
    unittest.main()
