# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of PR #495 (issue #474): migrate.sh's refusal and decoding against what libpq 18.6 (the pinned
image's psql) and pgx v5.11.0 (OpenFGA's) read, with the harness of tests/test_review_openfga_migrate.py. Docker and
both pinned images must already be present, as there."""

from __future__ import annotations

import unittest

import test_review_openfga_migrate as harness

SECRET = "Pw-474-ob3-not-a-secret"


class Review474OB3(harness.MigrateScript):
    container = f"{harness.MigrateScript.container}-474-ob3"

    def refused(self, uri: str) -> str:
        """migrate.sh on URI: refused at start-up in one log line (a psql run logs a line of its own, failed or not),
        no part of SECRET or the harness's password in it, and the database untouched."""
        code, log = self.migrate(uri=uri, connect_timeout="1")
        self.assertEqual(code, 1, log)
        self.assertEqual(len(log.splitlines()), 1, log)
        self.assertIn("the datastore URI (the Secret openfga-db-app, key uri) is refused", log)
        for secret in (SECRET, harness.PASSWORD):
            self.assertNotIn(secret[:10], log)
        self.assertEqual(self.sql("SELECT to_regclass('goose_db_version') IS NULL"), "t")
        return log

    def test_a_keyword_value_string_with_a_scheme_in_a_value_is_refused_before_psql(self):
        """libpq and pgx read a URI only after `postgresql://` or `postgres://` (fe-connect.c uri_prefix_length at
        REL_18_6; pgconn/parse_url.go:67-72 at v5.11.0), and anything else with an `=` as keyword=value. The refusal's
        `[^:/?#]+://` took `host=... password=... application_name=a://b` for a URI, so psql connected with the
        password on its command line and `openfga migrate` printed it whole ("invalid database uri: parse ...: first
        path segment in URL cannot contain colon", measured on 8aeb85bc)."""
        for uri in (f"host=127.0.0.1 port=5432 dbname=openfga user=openfga password={harness.PASSWORD} sslmode=disable"
                    " application_name=a://b",
                    f"host=127.0.0.1 port=5432 dbname=openfga user=openfga sslmode=disable password={SECRET}://x"):
            with self.subTest(uri=uri.replace(harness.PASSWORD, "...").replace(SECRET, "...")):
                self.refused(uri)

    def test_the_uri_cloudnativepg_writes_still_migrates(self):
        """The control for the scheme the refusal now requires: CloudNativePG's `uri`, postgresql:// with no query
        (pkg/specs/secrets.go at v1.30.1), is accepted and migrates."""
        code, log = self.migrate(uri=f"postgresql://openfga:{harness.PASSWORD}@127.0.0.1:5432/openfga")
        self.assertEqual(code, 0, log)
        self.assertNotIn(harness.PASSWORD, log)
        self.assertEqual(self.sql("SELECT max(version_id) FROM goose_db_version"), "6")


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """This file's tests only: its class inherits the harness's own, which run in their module."""
    suite = unittest.TestSuite()
    suite.addTests(Review474OB3(name) for name in sorted(vars(Review474OB3)) if name.startswith("test_"))
    return suite
