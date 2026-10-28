# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #325 (issue #305): what scripts/gen-realm-imports.py lets through into a KeycloakRealmImport.

At Keycloak 26.6.7, ``kc.sh import`` resolves every ``${...}`` of the realm text (Import.java turns replacement on;
AbstractFileBasedImportProvider reads System.getenv) in each form StringPropertyReplacer reads: ``${X}``,
``${X:default}`` and ``${X,Y}``. The operator's import Job is a copy of the Keycloak server's pod
(KeycloakRealmImportJobDependentResource takes the StatefulSet's template), so its environment holds the server's own
secrets (KC_DB_PASSWORD, KC_BOOTSTRAP_ADMIN_PASSWORD: KeycloakDistConfigurator) beside the two placeholders."""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from test_realm_imports import copy_inputs, generator_module


class PlaceholderGuard(unittest.TestCase):
    """The only ``${...}`` the realm may carry is a client's secret, exactly a placeholder the contract feeds."""

    def generate(self, display_name: str) -> dict:
        gen = generator_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            copy_inputs(root)
            realm = root / "deploy" / "keycloak" / "realm-inkcast.json"
            text = realm.read_text()
            self.assertIn('"displayName": "Inkcast"', text)
            realm.write_text(text.replace('"displayName": "Inkcast"', f'"displayName": "{display_name}"'))
            return gen.files(root)

    def refused(self, display_name: str) -> str:
        with self.assertRaises(SystemExit) as refused:
            self.generate(display_name)
        return str(refused.exception)

    def test_every_form_keycloak_resolves_from_the_environment_is_refused(self):
        """``${KC_DB_PASSWORD:none}`` would put the Keycloak database's password in the realm's display name."""
        for form in ("${KC_DB_PASSWORD:none}", "${KC_DB_PASSWORD,KC_NOPE}"):
            with self.subTest(form=form):
                self.assertIn("KC_DB_PASSWORD", self.refused(form))

    def test_a_fed_secret_is_refused_outside_its_client(self):
        """The display name is on the public sign-in page: a client secret must never be resolved there."""
        self.assertIn("KC_INKCAST_SYNC_SECRET", self.refused("${KC_INKCAST_SYNC_SECRET}"))

    def test_keycloak_message_keys_still_pass(self):
        """Keycloak's own message keys (``${client_account}``) name no variable and stay as they are: still allowed."""
        self.assertEqual(len(self.generate("${client_account}")), 2)


if __name__ == "__main__":
    unittest.main()
