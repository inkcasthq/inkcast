# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The review of PRs #452 (issue #410) and #453 (issue #409), and the concerns it left (issue #474):
components/openfga/migrate.sh against the pinned PostgreSQL and OpenFGA images, with the harness of
tests/test_review_openfga_migrate.py (its container, its database and its runner). Docker and both images must
already be present, as there."""

from __future__ import annotations

import re
import subprocess
import time
import unittest

import test_review_openfga_migrate as harness


class Review410(harness.MigrateScript):
    container = f"{harness.MigrateScript.container}-4xx-r"

    def test_a_raw_question_mark_or_hash_in_the_password_reaches_no_log_and_no_command_line(self):
        """libpq ends a URI's userinfo at its first `@` before any `/` (fe-connect.c conninfo_uri_parse_options at
        REL_18_6), and pgx does the same (pgconn/parse_url.go at v5.11.0), so a raw `?` or `#` in the password is
        part of it: psql connects and the server serves. Go's net/url ends the authority there instead and prints
        the URI ("invalid port ... after host"). migrate.sh's split must take what libpq takes, or psql gets the
        whole URI on its command line and `openfga migrate` prints the password, all of it before a `#`."""
        for mark in ("?", "#"):
            password = f"Pw-410-review{mark}not-a-secret"
            with self.subTest(password=password.replace("not-a-secret", "...")):
                self.setUp()
                self.sql(f"ALTER ROLE openfga PASSWORD '{password}'", "postgres")
                self.addCleanup(self.sql, f"ALTER ROLE openfga PASSWORD '{harness.PASSWORD}'", "postgres")
                uri = f"postgres://openfga:{password}@127.0.0.1:5432/openfga?sslmode=disable"
                key = re.search(r'lock_key="\$\{LOCK_KEY:-(\d+)\}"', harness.SCRIPT.read_text())[1]
                holder = subprocess.Popen(["docker", "exec", self.container, "psql", "-h", "/tmp", "-U", "postgres", "-d", "openfga",
                                           "-Atc", f"SELECT pg_advisory_lock({key}), pg_sleep(3)"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.addCleanup(holder.wait, 60)
                runner = subprocess.Popen(self.migrate_command(uri=uri), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                session = []
                for _ in range(40):   # the session psql, while the runner waits for the lock the holder keeps
                    session = self.command_lines_holding("psql -X -q -At")
                    if session:
                        break
                    time.sleep(0.25)
                leaked = self.command_lines_holding("not-a-secret")
                log, _ = runner.communicate(timeout=120)
                self.assertTrue(session, "the runner's psql session was never seen")
                self.assertEqual(leaked, [], "the password in a command line")
                self.assertNotIn("Pw-410-review", log, "the password in the init container's log")
                self.assertNotIn("not-a-secret", log, "the password in the init container's log")
                self.assertEqual(runner.returncode, 0, log)
                self.assertEqual(self.sql("SELECT max(version_id) FROM goose_db_version"), "6")


class Review409(harness.MigrateScript):
    container = f"{harness.MigrateScript.container}-4xx-r-409"

    def migrate_with(self, **env: str) -> tuple[int, str]:
        """migrate.sh as the init container runs it, with ENV added to its environment: its exit code and its log."""
        command = self.migrate_command()
        command[-3:-3] = [f"-e{name}={value}" for name, value in env.items()]   # after the defaults, which they override
        done = subprocess.run(command, capture_output=True, text=True, timeout=300)
        return done.returncode, done.stdout + done.stderr

    def test_a_probe_interval_outside_whole_seconds_from_1_to_4_is_refused_before_the_database(self):
        """PROBE_SECONDS paces the probe and sizes the hand-over grace (2 x PROBE_SECONDS + 2). 0, a negative or a word
        made `while sleep ...; kill -0 ...` a busy loop (sleep fails at once, the loop tests only kill -0): about
        2,000 probes a second, measured; a fraction or a unit, which sleep takes, broke the grace's arithmetic, and
        bash skipped the grace with its `if` (measured: "0.5: syntax error: invalid arithmetic operator", exit 0).
        And at half pace the grace is the time a runner whose lock died still migrates: above 4 s it passes the 10 s
        of the issue. So the script refuses such a value at start-up, naming the variable, before it connects."""
        for value in ("0", "-1", "abc", "0.5", "2s", "5"):
            with self.subTest(value=value):
                code, log = self.migrate_with(PROBE_SECONDS=value)
                self.assertEqual(code, 1, log)
                self.assertIn("PROBE_SECONDS", log)
                self.assertNotIn("database reachable", log)
                self.assertEqual(self.sql("SELECT to_regclass('goose_db_version') IS NULL"), "t")

    def test_a_run_ends_when_its_migration_does_not_a_probe_interval_later(self):
        """The normal path, unchanged: the probe loop slept its interval before it looked at `openfga migrate`, so
        every run, a schema already current included, ended up to PROBE_SECONDS after its migration (measured with
        the default 2 s: 2.08 s a run against 0.10 s before #453, over 30 runs), and with `strategy: Recreate` that
        is downtime at every rollout. The run ends with the migration, with OpenFGA's own exit code."""
        self.assertMigrated()
        started = time.monotonic()
        code, log = self.migrate_with(PROBE_SECONDS="4")
        took = time.monotonic() - started
        self.assertEqual(code, 0, log)
        self.assertIn('"current version":6', log)
        self.assertLess(took, 2, f"a run on a current schema took {took:.1f} s with PROBE_SECONDS=4")
        code, log = self.migrate_with(PROBE_SECONDS="4", OPENFGA_DATASTORE_ENGINE="review-409")
        self.assertEqual(code, 1, log)
        self.assertIn("unknown datastore engine type: review-409", log)
        self.assertIn("openfga migrate exited 1: this run fails", log)


class Review474(harness.MigrateScript):
    container = f"{harness.MigrateScript.container}-474"

    def refused(self, uri: str) -> str:
        """migrate.sh on URI: refused at start-up, its log the refusal's one line (a psql run logs a line of its own,
        failed or not) and the database untouched."""
        code, log = self.migrate(uri=uri, connect_timeout="1")
        self.assertEqual(code, 1, log)
        self.assertEqual(len(log.splitlines()), 1, log)
        self.assertIn("the datastore URI (the Secret openfga-db-app, key uri) is refused", log)
        self.assertEqual(self.sql("SELECT to_regclass('goose_db_version') IS NULL"), "t")
        return log

    def test_a_uri_the_parsers_read_differently_is_refused_before_psql_with_no_part_of_its_password(self):
        """K1: libpq ends the user information at the first `@` before any `/` (fe-connect.c
        conninfo_uri_parse_options at REL_18_6), so a raw `@` in the password puts what follows it in the host
        (`could not translate host name "...@127.0.0.1"`) and a raw `/` puts what precedes it in the port (`invalid
        integer value "..." for connection option "port"`); Go's net/url refuses the `/`. No split can recover such a
        password, and the wait for the database logged libpq's error, part of the password in it. A keyword=value
        string is no URI at all: psql took it whole, password included, on its command line for the whole run, and
        `openfga migrate` then failed on it (measured on 4fa484fe)."""
        for uri in (*(f"postgres://openfga:{password}@127.0.0.1:5432/openfga?sslmode=disable"
                      for password in ("Pw-474@not-a-secret", "Pw-474/not-a-secret", "Pw-474@a/not-a-secret")),
                    "host=127.0.0.1 port=5432 dbname=openfga user=openfga password=Pw-474-not-a-secret sslmode=disable"):
            with self.subTest(uri=uri.replace("not-a-secret", "...")):
                log = self.refused(uri)
                self.assertIn("percent-encoded", log)
                self.assertNotIn("Pw-474", log)
                self.assertNotIn("not-a-secret", log)

    def test_a_password_in_the_query_is_refused_before_psql(self):
        """K3: libpq and pgx honour a password in the query, over the user information's, and decode a keyword as they
        decode a value (conninfo_uri_parse_params at REL_18_6), so migrate.sh's split left it in the URI psql runs
        with, on a command line every user of the node reads (the role's password is the query's here, so that
        before #474 the run connected with it and migrated). sslpassword, a client key's, is a password as well."""
        secret = "Pw-474-not-a-secret"
        self.sql(f"ALTER ROLE openfga PASSWORD '{secret}'", "postgres")
        self.addCleanup(self.sql, f"ALTER ROLE openfga PASSWORD '{harness.PASSWORD}'", "postgres")
        for uri in (f"postgres://openfga@127.0.0.1:5432/openfga?sslmode=disable&password={secret}",
                    f"postgres://openfga@127.0.0.1:5432/openfga?sslmode=disable&pass%77ord={secret}",
                    f"postgres://openfga:{harness.PASSWORD}@127.0.0.1:5432/openfga?password={secret}",
                    f"postgres://openfga@127.0.0.1:5432/openfga?sslmode=disable&sslpassword={secret}"):
            with self.subTest(uri=uri.replace(secret, "...").replace(harness.PASSWORD, "...")):
                log = self.refused(uri)
                self.assertIn("a password or a secret in its query", log)
                self.assertNotIn("not-a-secret", log)

    def test_a_secret_key_or_options_in_any_case_is_refused_before_psql(self):
        """The integrator's, on K3 and K8: libpq 18.6 holds two more secrets (oauth_client_secret, shown as "*") and two
        keys (scram_client_key, scram_server_key, hidden as "D") in its option table (fe-connect.c PQconninfoOptions at
        REL_18_6), and compares a keyword with strcmp (conninfo_find), so an upper-case PASSWORD= is refused by psql
        only after it starts, its value on the command line. Every case of each key is refused before psql runs."""
        secret = "Pw-474-not-a-secret"
        for query in (f"PASSWORD={secret}", f"SslPassword={secret}", f"oauth_client_secret={secret}",
                      f"scram_client_key={secret}", f"SCRAM_SERVER_KEY={secret}"):
            with self.subTest(query=query.replace(secret, "...")):
                log = self.refused(f"postgres://openfga:{harness.PASSWORD}@127.0.0.1:5432/openfga?sslmode=disable&{query}")
                self.assertIn("a password or a secret in its query", log)
                self.assertNotIn("not-a-secret", log)
        log = self.refused(f"postgres://openfga:{harness.PASSWORD}@127.0.0.1:5432/openfga?sslmode=disable&OPTIONS=-c%20work_mem%3D1MB")
        self.assertIn("options=", log)

    def test_a_backslash_in_the_password_migrates(self):
        """K4: libpq decodes %XX alone and keeps a backslash as it is (conninfo_uri_decode at REL_18_6), while
        migrate.sh's `printf '%b'` read a raw `\\n` as a newline and stopped at `\\c`, so psql failed authentication on
        every run while the server accepted the same URI. Percent-encoded, as env-secrets writes it, it still migrates."""
        password = r"Pw\n474\c-not-a-secret"
        self.sql(f"ALTER ROLE openfga PASSWORD '{password}'", "postgres")   # standard_conforming_strings: as written
        self.addCleanup(self.sql, f"ALTER ROLE openfga PASSWORD '{harness.PASSWORD}'", "postgres")
        for written in (password, password.replace("\\", "%5C")):
            with self.subTest(encoded="%5C" in written):
                self.setUp()
                code, log = self.migrate(uri=f"postgres://openfga:{written}@127.0.0.1:5432/openfga?sslmode=disable",
                                         connect_timeout="5")
                self.assertEqual(code, 0, log)
                self.assertNotIn("not-a-secret", log)
                self.assertEqual(self.sql("SELECT max(version_id) FROM goose_db_version"), "6")

    def test_options_in_the_query_is_refused_before_psql(self):
        """K8: a URI's options= replaces PGOPTIONS whole, for psql as for pgx (pgconn/config.go mergeSettings at
        v5.11.0: the URI after the environment), and with it client_connection_check_interval, by which a killed
        runner's statement ends with its client (measured with psql 18.6: 2s from PGOPTIONS alone, 0 with
        `options=-c%20work_mem%3D1MB` in the URI). No writer of the Secret sets one: env-secrets writes sslmode=require
        alone (env_secrets.py, Layout.uri) and CloudNativePG no query at all (pkg/specs/secrets.go at v1.30.1)."""
        for query in ("options=-c%20work_mem%3D1MB", "opt%69ons=-c%20work_mem%3D1MB"):
            with self.subTest(query=query):
                log = self.refused(f"postgres://openfga:{harness.PASSWORD}@127.0.0.1:5432/openfga?sslmode=disable&{query}")
                self.assertIn("options=", log)
                self.assertNotIn(harness.PASSWORD, log)


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """This file's tests only: its classes inherit the harness's own, which run in their module."""
    suite = unittest.TestSuite()
    for case in (Review410, Review409, Review474):
        suite.addTests(case(name) for name in sorted(vars(case)) if name.startswith("test_"))
    return suite
