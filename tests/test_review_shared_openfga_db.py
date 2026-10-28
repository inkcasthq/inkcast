# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #304 (issue #296): what tests/test_shared_openfga_db.py leaves unproved, each case failing on the
code it was written against."""

from __future__ import annotations

import unittest

from test_shared_openfga_db import CREDENTIALS, FakeOcCase, uri

# A failing `oc` in front of the recording fake: the one call whose arguments contain $OC_FAIL_ON is refused the way
# the API server refuses it; every other call goes to the fake.
_FAILING_OC = r"""#!/usr/bin/env bash
if [ -n "${OC_FAIL_ON:-}" ] && [[ "$*" == *"$OC_FAIL_ON"* ]]; then
  echo "$*" >> "$CALLS"
  echo 'Error from server (Forbidden): secrets is forbidden: User "developer" cannot get resource "secrets"' >&2
  exit 1
fi
exec "$(dirname "$0")/oc-fake" "$@"
"""


class CredentialsScriptWhenOcFailsMidRun(FakeOcCase):
    """An agreeing pair is read value by value; an oc failure on any of those reads is oc's own error, never an empty
    value read as "disagrees" or "not a basic-auth Secret" (which would tell the operator to delete both Secrets and
    generate a new password, cutting the running OpenFGA off for an API blip)."""

    PW = "e" * 48

    def setUp(self):
        super().setUp()
        fake = self.dir / "bin" / "oc"
        fake.rename(fake.with_name("oc-fake"))
        fake.write_text(_FAILING_OC)
        fake.chmod(0o755)
        self.seed("inkcast-db", "inkcast-pg-openfga-staging", "kubernetes.io/basic-auth",
                  username="openfga_staging", password=self.PW)
        self.seed("inkcast-staging", "openfga-db-app", "Opaque",
                  username="openfga_staging", password=self.PW, uri=uri("staging", self.PW))

    def test_the_agreeing_pair_is_kept_when_oc_answers(self):
        done = self.run_script(CREDENTIALS, "staging")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Kept", done.stdout)

    def test_an_oc_failure_while_reading_the_pair_is_oc_s_own_error(self):
        reads = [f"-n {namespace} get secret {name} --ignore-not-found -o jsonpath={{.data.{key}}}"
                 for namespace, name, key in (("inkcast-db", "inkcast-pg-openfga-staging", "password"),
                                              ("inkcast-db", "inkcast-pg-openfga-staging", "username"),
                                              ("inkcast-staging", "openfga-db-app", "password"),
                                              ("inkcast-staging", "openfga-db-app", "username"),
                                              ("inkcast-staging", "openfga-db-app", "uri"))]
        for failing in reads:
            with self.subTest(failing=failing):
                self.env["OC_FAIL_ON"] = failing
                done = self.run_script(CREDENTIALS, "staging")
                self.assertIn(failing, self.calls(), "the read was attempted")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("Error from server (Forbidden)", done.stderr)
                for misread in ("disagrees", "is not a basic-auth Secret", "Delete both", "Kept"):
                    self.assertNotIn(misread, done.stderr + done.stdout)
                self.assertFalse([c for c in self.calls() if "create" in c or "replace" in c or "delete" in c])
                self.assert_never_seen(self.PW, done)


if __name__ == "__main__":
    unittest.main()
