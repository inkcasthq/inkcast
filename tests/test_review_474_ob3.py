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

    def test_a_question_mark_or_hash_in_the_user_name_is_refused_before_psql(self):
        """libpq ends the user name at `:` or `@` alone (conninfo_uri_parse_options at REL_18_6), so in
        `openfga?x=y@127.0.0.1/openfga?password=...` the user name is `openfga?x=y` and the query starts at the `?`
        after the path; the query loop started it at the first `?` and read `x` as the only key, so the password
        stayed on psql's command line (measured on 8aeb85bc with a stub psql: its argument held it). Go's net/url, which
        `openfga migrate` parses its URI with, ends the authority at that first `?` or `#` instead, so it read another
        host than psql's. Only the password, which migrate.sh removes, may hold them."""
        for uri in (f"postgres://openfga?x=y@127.0.0.1:5432/openfga?password={SECRET}&sslmode=disable",
                    f"postgres://openfga?x=y:{harness.PASSWORD}@127.0.0.1:5432/openfga?password={SECRET}&sslmode=disable",
                    "postgres://openfga#x@127.0.0.1:5432/openfga?sslmode=disable"):
            with self.subTest(uri=uri.replace(harness.PASSWORD, "...").replace(SECRET, "...")):
                self.refused(uri)

    def test_a_control_character_outside_the_password_is_refused_before_psql(self):
        """`read` stops at a newline, so the query loop never saw a parameter after one, while libpq reads on: psql
        connected with a password after it on its command line, and `openfga migrate` printed the URI, password and
        all ("invalid database uri: parse ...: net/url: invalid control character in URL", measured on 8aeb85bc), as
        it prints any URI with a control character, so none can ever migrate. An options= after one replaced
        PGOPTIONS. A trailing newline, which `echo ... > file` leaves, failed for CONNECT_TIMEOUT on libpq's `invalid
        sslmode value: "disable\\n"`."""
        base = "postgres://openfga@127.0.0.1:5432/openfga?sslmode=disable&application_name=x"
        for uri in (f"{base}\n&password={harness.PASSWORD}", f"{base}\n&options=-c%20client_connection_check_interval%3D0",
                    f"postgres://openfga:{harness.PASSWORD}@127.0.0.1:5432/openfga?sslmode=disable\n"):
            with self.subTest(uri=uri.replace(harness.PASSWORD, "...")):
                self.refused(uri)

    def test_a_control_character_in_the_password_still_migrates(self):
        """The control for the refusal above: the password leaves the URI before anything parses it (PGPASSWORD, and
        OPENFGA_DATASTORE_PASSWORD, which OpenFGA encodes itself), so a raw tab in it, which libpq and pgx keep,
        still migrates."""
        password = f"{SECRET}\tx"
        self.sql(f"ALTER ROLE openfga PASSWORD E'{SECRET}\\tx'", "postgres")
        self.addCleanup(self.sql, f"ALTER ROLE openfga PASSWORD '{harness.PASSWORD}'", "postgres")
        code, log = self.migrate(uri=f"postgres://openfga:{password}@127.0.0.1:5432/openfga?sslmode=disable")
        self.assertEqual(code, 0, log)
        self.assertNotIn(SECRET, log)
        self.assertEqual(self.sql("SELECT max(version_id) FROM goose_db_version"), "6")

    def test_a_raw_space_at_either_end_of_the_password_is_dropped_as_libpq_and_pgx_drop_it(self):
        """conninfo_uri_decode (fe-connect.c at REL_18_6) and pgx's uriDecode (pgconn/parse_url.go:304-355 at
        v5.11.0) drop a component's leading and trailing raw spaces, so the server, which parses the whole URI with
        pgx, takes this password without them. uri_decode kept them, and psql failed authentication on every run
        (measured on 8aeb85bc), K4's failure the other way round."""
        for written in (f"{harness.PASSWORD} ", f" {harness.PASSWORD}"):
            with self.subTest(space="trailing" if written.endswith(" ") else "leading"):
                self.setUp()
                code, log = self.migrate(uri=f"postgres://openfga:{written}@127.0.0.1:5432/openfga?sslmode=disable",
                                         connect_timeout="5")
                self.assertEqual(code, 0, log)
                self.assertNotIn(harness.PASSWORD, log)
                self.assertEqual(self.sql("SELECT max(version_id) FROM goose_db_version"), "6")

    def test_a_password_libpq_cannot_decode_is_refused_before_psql(self):
        """A raw space inside the password, a `%` without two hex digits after it, and `%00` make libpq and pgx refuse
        the URI, libpq quoting the password in its error (conninfo_uri_decode). uri_decode made another password of
        each, and psql failed authentication for CONNECT_TIMEOUT (measured on 8aeb85bc)."""
        for written in (f"{SECRET} x", f"{SECRET}%4g", f"{SECRET}%", f"{SECRET}%4", f"{SECRET}%00x"):
            with self.subTest(written=written.replace(SECRET, "...")):
                log = self.refused(f"postgres://openfga:{written}@127.0.0.1:5432/openfga?sslmode=disable")
                self.assertIn("%20", log)


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """This file's tests only: its class inherits the harness's own, which run in their module."""
    suite = unittest.TestSuite()
    suite.addTests(Review474OB3(name) for name in sorted(vars(Review474OB3)) if name.startswith("test_"))
    return suite
