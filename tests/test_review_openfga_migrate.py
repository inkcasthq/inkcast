# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The review of PR #374 (E3.F3.9.S8, issue #337): components/openfga/migrate.sh run as the init container runs it,
against a real PostgreSQL and OpenFGA's own binary. One container of the pinned PostgreSQL image (the init
container's, deployment.yaml) serves the database and runs the script, with the pinned OpenFGA image mounted at
/opt/openfga as the pod's image volume mounts it. Docker and both images must already be present: a unit test pulls
nothing, and the kind job proves the pod itself (scripts/dev/openfga-e2e.sh)."""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import time
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "deploy" / "kustomize" / "components" / "openfga"
SCRIPT = COMPONENT / "migrate.sh"
PASSWORD = "Pw-review-374-not-a-secret"
URI = f"postgres://openfga:{PASSWORD}@127.0.0.1:5432/openfga?sslmode=disable"


def pinned_images() -> tuple[str, str]:
    """The migrate container's image and the image volume's reference, as name@digest (a local image may be untagged)."""
    pod = yaml.safe_load((COMPONENT / "deployment.yaml").read_text())["spec"]["template"]["spec"]
    (migrate,) = [c for c in pod["initContainers"] if c["name"] == "migrate"]
    (volume,) = [v for v in pod["volumes"] if v["name"] == "openfga"]
    return untag(migrate["image"]), untag(volume["image"]["reference"])


def untag(reference: str) -> str:
    return re.sub(r":[^:/@]+@", "@", reference)


def docker_ready() -> str:
    """Why the harness cannot run here, or the empty string."""
    if not shutil.which("docker"):
        return "no docker"
    if subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode:
        return "no docker daemon"
    for image in pinned_images():
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30).returncode:
            return f"{image} is not present locally (a unit test pulls nothing)"
    return ""


UNAVAILABLE = docker_ready()


class TheHarnessRuns(unittest.TestCase):
    """Where the harness is required (CI's kind job, scripts/dev/openfga-migration-harness.sh, issue #422), a harness that
    cannot run fails here: every test below would skip, and a change to migrate.sh that breaks them would pass."""

    @unittest.skipUnless(os.environ.get("INKCAST_MIGRATE_HARNESS") == "required", "the harness is not required here")
    def test_docker_and_both_pinned_images_are_present(self):
        self.assertEqual(UNAVAILABLE, "")


@unittest.skipIf(UNAVAILABLE, UNAVAILABLE)
class MigrateScript(unittest.TestCase):
    container = f"inkcast-review-374-{os.getpid()}"

    @classmethod
    def setUpClass(cls):
        postgres, openfga = pinned_images()
        subprocess.run(["docker", "run", "-d", "--rm", "--name", cls.container,
                        "--mount", f"type=image,source={openfga},target=/opt/openfga", postgres, "bash", "-c",
                        "initdb -D /tmp/pg -U postgres --auth-local=trust --auth-host=scram-sha-256 >/dev/null"
                        " && exec postgres -D /tmp/pg -c listen_addresses=127.0.0.1 -k /tmp"],
                       check=True, capture_output=True, timeout=120)
        cls.addClassCleanup(subprocess.run, ["docker", "rm", "-f", cls.container], capture_output=True, timeout=60)
        subprocess.run(["docker", "cp", str(SCRIPT), f"{cls.container}:/tmp/migrate.sh"], check=True, capture_output=True)
        for _ in range(60):
            if not subprocess.run(["docker", "exec", cls.container, "psql", "-h", "/tmp", "-U", "postgres", "-Atc", "SELECT 1"],
                                  capture_output=True).returncode:
                break
            time.sleep(1)
        cls.sql(f"CREATE ROLE openfga LOGIN PASSWORD '{PASSWORD}'", "postgres")

    def setUp(self):
        self.sql("DROP DATABASE IF EXISTS openfga WITH (FORCE)", "postgres")
        self.sql("CREATE DATABASE openfga OWNER openfga", "postgres")

    @classmethod
    def sql(cls, statement: str, database: str = "openfga") -> str:
        """One statement as the superuser, over the socket: its result, tuples only."""
        done = subprocess.run(["docker", "exec", cls.container, "psql", "-h", "/tmp", "-U", "postgres", "-d", database,
                               "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-c", statement],
                              capture_output=True, text=True, timeout=120)
        if done.returncode:
            raise AssertionError(f"{statement}: {done.stderr}")
        return done.stdout.strip()

    @staticmethod
    def migrate_env(uri: str = URI, connect_timeout: str = "20") -> list[str]:
        """The init container's environment (deployment.yaml), as docker's -e arguments."""
        env = {"OPENFGA_DATASTORE_ENGINE": "postgres", "OPENFGA_DATASTORE_URI": uri, "OPENFGA_LOG_FORMAT": "json",
               "OPENFGA_TIMEOUT": "5s", "CONNECT_TIMEOUT": connect_timeout}
        return [f"-e{k}={v}" for k, v in env.items()]

    def migrate_command(self, uri: str = URI, connect_timeout: str = "20") -> list[str]:
        return ["docker", "exec", *self.migrate_env(uri, connect_timeout), self.container, "bash", "/tmp/migrate.sh"]

    def migrate(self, **kwargs) -> tuple[int, str]:
        done = subprocess.run(self.migrate_command(**kwargs), capture_output=True, text=True, timeout=300)
        return done.returncode, done.stdout + done.stderr

    def assertMigrated(self) -> str:
        code, log = self.migrate()
        self.assertEqual(code, 0, log)
        return log

    def test_a_version_above_6_keeps_its_version_6_rows(self):
        """The repair deletes goose's version-6 rows so that 006 runs again, which goose does only for a file above the
        table's highest version (up.go:99-106 at v3.28.0). With a later version recorded, a deleted 6 is a migration
        missing below it, and goose refuses that on every run (up.go:77-90: "found 1 missing migrations before
        current version 7"), so the init container would fail for good where `openfga migrate` alone succeeds."""
        self.assertMigrated()
        self.sql("INSERT INTO goose_db_version (version_id, is_applied) VALUES (7, true)")   # a later OpenFGA's 007
        self.sql("DROP INDEX idx_user_lookup")                                               # which that 007 replaced
        for run in (1, 2):
            with self.subTest(run=run):
                code, log = self.migrate()
                self.assertNotIn("missing migrations", log)
                self.assertEqual(code, 0, log)
                self.assertEqual(self.sql("SELECT count(*) FROM goose_db_version WHERE version_id = 6"), "1")

    def test_an_invalid_index_at_version_6_is_still_repaired(self):
        """What the repair is for, kept: an INVALID idx_user_lookup (a concurrent build that failed, create_index.sgml)
        with version 6 recorded is dropped with its rows, and 006 builds it again."""
        self.assertMigrated()
        self.sql("INSERT INTO tuple (store, object_type, object_id, relation, _user, user_type, ulid, inserted_at)"
                 " VALUES ('review', 'document', 'd', 'viewer', 'user:u', 'user', '01REVIEW374000000000000000', now())")
        self.sql("DROP INDEX idx_user_lookup")
        with self.assertRaises(AssertionError):
            self.sql("CREATE INDEX CONCURRENTLY idx_user_lookup ON tuple ((1 / (0 * length(store))))")
        self.sql("DELETE FROM tuple WHERE store = 'review'")
        self.assertEqual(self.sql("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')"), "f")
        log = self.assertMigrated()
        self.assertIn("repair: idx_user_lookup is INVALID", log)
        self.assertIn('"current version":5', log)
        self.assertEqual(self.sql("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')"), "t")
        self.assertEqual(self.sql("SELECT count(*) FROM goose_db_version WHERE version_id = 6"), "1")

    def command_lines_holding(self, needle: str) -> list[str]:
        """Every command line in the container that holds NEEDLE, read from /proc there (the needle travels in the
        probe's environment, so the probe's own command line never matches)."""
        probe = ('for p in /proc/[0-9]*; do c="$(tr "\\0" " " < "$p/cmdline" 2>/dev/null)";'
                 ' [[ "$c" == *"$NEEDLE"* ]] && echo "$c"; done; true')
        done = subprocess.run(["docker", "exec", "-e", f"NEEDLE={needle}", self.container, "bash", "-c", probe],
                              capture_output=True, text=True, timeout=60, check=True)
        return done.stdout.splitlines()

    def test_the_password_reaches_no_command_line_and_no_log(self):
        """The psql session that holds the lock lives for the whole run, and a process's command line is readable by
        every user of the node (/proc/<pid>/cmdline is mode 0444; `ps`), while its environment is its own (environ,
        0400): the password must not be in psql's argv. And libpq quotes a URI it cannot parse, or the token it
        cannot decode, in its error, which migrate.sh logs while it waits for the database."""
        key = re.search(r'lock_key="\$\{LOCK_KEY:-(\d+)\}"', SCRIPT.read_text())[1]
        holder = subprocess.Popen(["docker", "exec", self.container, "psql", "-h", "/tmp", "-U", "postgres", "-d", "openfga",
                                   "-Atc", f"SELECT pg_advisory_lock({key}), pg_sleep(5)"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(holder.wait, 60)
        runner = subprocess.Popen(self.migrate_command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        session = []
        for _ in range(40):   # the session psql, while the runner waits for the lock the holder keeps
            session = self.command_lines_holding("psql -X -q -At")
            if session:
                break
            time.sleep(0.25)
        leaked = self.command_lines_holding(PASSWORD)
        log, _ = runner.communicate(timeout=120)
        self.assertTrue(session, "the runner's psql session was never seen")
        self.assertEqual(leaked, [], "the password in a command line")
        self.assertEqual(runner.returncode, 0, log)
        self.assertIn("waiting for the migration lock", log)
        self.assertNotIn(PASSWORD, log)
        for uri in (f"postgres://openfga:{PASSWORD}%zz@127.0.0.1:5432/openfga",   # a token libpq cannot decode
                    f"postgres://openfga:{PASSWORD}@[::1:5432/openfga"):            # a URI libpq cannot parse
            with self.subTest(uri=uri.replace(PASSWORD, "<password>")):
                code, log = self.migrate(uri=uri, connect_timeout="1")
                self.assertEqual(code, 1, log)
                self.assertIn("no database after 1 s", log)
                self.assertNotIn(PASSWORD, log)

    def test_a_killed_runner_leaves_no_statement_running_without_the_lock(self):
        """A pod deleted while it migrates: SIGTERM ends bash, PID 1, at once (measured: exit 143 in under a second)
        and the kubelet kills the rest. The lock goes with psql's session, but OpenFGA's own backend keeps running
        its statement, here 006's CREATE INDEX CONCURRENTLY, with no lock and no client, until it next talks to
        that client; the next runner's repair then races it. PostgreSQL checks a session's client while a statement
        runs when the session asks (client_connection_check_interval)."""
        self.assertMigrated()
        self.sql("DELETE FROM goose_db_version WHERE version_id = 6")
        self.sql("DROP INDEX idx_user_lookup")
        # An open snapshot: 006's concurrent build waits for it, as a large table keeps a build running.
        snapshot = subprocess.Popen(["docker", "exec", self.container, "psql", "-h", "/tmp", "-U", "postgres", "-d", "openfga",
                                     "-c", "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM goose_db_version;"
                                     " SELECT pg_sleep(60); COMMIT;"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end_snapshot = ("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                        " WHERE query LIKE '%pg_sleep(60)%' AND pid <> pg_backend_pid()")
        self.addCleanup(snapshot.wait, 60)
        self.addCleanup(self.sql, end_snapshot)
        time.sleep(1)
        # The runner as a pod runs it: migrate.sh as PID 1 of its own container, in the database's network namespace.
        postgres, openfga = pinned_images()
        runner = f"{self.container}-runner"
        subprocess.run(["docker", "create", "--name", runner, "--network", f"container:{self.container}",
                        "--mount", f"type=image,source={openfga},target=/opt/openfga", *self.migrate_env(),
                        postgres, "bash", "/tmp/migrate.sh"], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", runner], capture_output=True, timeout=60)
        subprocess.run(["docker", "cp", str(SCRIPT), f"{runner}:/tmp/migrate.sh"], check=True, capture_output=True)
        subprocess.run(["docker", "start", runner], check=True, capture_output=True)
        building = "SELECT count(*) FROM pg_stat_progress_create_index"
        for _ in range(80):
            if self.sql(building) == "1":
                break
            time.sleep(0.25)
        self.assertEqual(self.sql(building), "1", "006's build never started")
        subprocess.run(["docker", "kill", "-s", "TERM", runner], check=True, capture_output=True)   # as the kubelet does
        running = "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'openfga-migrate' AND state = 'active'"
        killed = time.monotonic()
        while self.sql(running) != "0" and time.monotonic() - killed < 10:
            time.sleep(0.5)
        self.assertEqual(self.sql("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"), "0")
        self.assertEqual(self.sql(running), "0", "a migration statement still runs 10 s after its runner was killed, without the lock")
        self.sql(end_snapshot)
        log = self.assertMigrated()   # the next run, in one go: the repair drops the invalid index, 006 builds it
        self.assertIn("repair: idx_user_lookup is INVALID", log)
        self.assertEqual(self.sql("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')"), "t")
        self.assertEqual(self.sql("SELECT count(*) FROM goose_db_version WHERE version_id = 6"), "1")

    def test_a_lock_session_that_dies_alone_ends_the_migration_within_10_s(self):
        """Issue #409: the psql session that holds the lock can die while its runner lives (pg_terminate_backend,
        SIGTERM to that one backend, signalfuncs.c:237-251 at REL_18_6; a middlebox dropping an idle connection),
        and the lock goes with it ("held until explicitly released or the session ends", mvcc.sgml "Advisory Locks"),
        while `openfga migrate` ran on, unlocked, to the end, beside whichever runner took the lock next. migrate.sh
        now probes the session while `openfga migrate` runs in the background, ends the run when it is gone and
        kills the migration; its backend's statement in flight, here 006's CREATE INDEX CONCURRENTLY held by a
        snapshot, ends with its client (client_connection_check_interval, the F4 of PR #374). The run exits non-zero
        and leaves no process, and the next run repairs what the interrupted 006 left."""
        self.assertMigrated()
        self.sql("DELETE FROM goose_db_version WHERE version_id = 6")
        self.sql("DROP INDEX idx_user_lookup")
        snapshot = subprocess.Popen(["docker", "exec", self.container, "psql", "-h", "/tmp", "-U", "postgres", "-d", "openfga",
                                     "-c", "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM goose_db_version;"
                                     " SELECT pg_sleep(60); COMMIT;"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end_snapshot = ("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                        " WHERE query LIKE '%pg_sleep(60)%' AND pid <> pg_backend_pid()")
        self.addCleanup(snapshot.wait, 60)
        self.addCleanup(self.sql, end_snapshot)
        time.sleep(1)
        runner = subprocess.Popen(self.migrate_command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(runner.wait, 120)
        self.addCleanup(runner.kill)
        building = "SELECT count(*) FROM pg_stat_progress_create_index"
        for _ in range(80):
            if self.sql(building) == "1":
                break
            time.sleep(0.25)
        self.assertEqual(self.sql(building), "1", "006's build never started")
        # The lock session alone: the one backend that holds the advisory lock; OpenFGA's own connections live on.
        self.assertEqual(self.sql("SELECT count(pg_terminate_backend(pid)) FROM pg_locks WHERE locktype = 'advisory' AND granted"), "1")
        died = time.monotonic()
        running = "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'openfga-migrate' AND state = 'active'"
        while self.sql(running) != "0" and time.monotonic() - died < 10:
            time.sleep(0.5)
        self.assertEqual(self.sql("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"), "0")
        self.assertEqual(self.sql(running), "0", "a migration statement still runs 10 s after its lock session died, without the lock")
        try:
            log, _ = runner.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the run did not end within 10 s of its lock session's death")
        self.assertNotEqual(runner.returncode, 0, log)
        self.assertIn("the database session ended", log)
        self.assertEqual(self.command_lines_holding("/opt/openfga/openfga migrate"), [], "openfga migrate left running")
        self.sql(end_snapshot)
        log = self.assertMigrated()   # the next run: the repair drops the invalid index, 006 builds it
        self.assertIn("repair: idx_user_lookup is INVALID", log)
        self.assertEqual(self.sql("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')"), "t")
        self.assertEqual(self.sql("SELECT count(*) FROM goose_db_version WHERE version_id = 6"), "1")


@unittest.skipIf(UNAVAILABLE, UNAVAILABLE)
class PartitionedRunner(unittest.TestCase):
    """A database and a runner in two containers on their own network, so that the runner can be cut off."""

    def docker(self, *args: str, check: bool = True) -> str:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120, check=check).stdout.strip()

    def sql(self, statement: str, database: str = "openfga") -> str:
        return self.docker("exec", self.server, "psql", "-h", "/tmp", "-U", "postgres", "-d", database, "-X", "-q", "-At",
                           "-v", "ON_ERROR_STOP=1", "-c", statement)

    def test_a_runner_cut_off_releases_the_lock_within_a_minute(self):
        """A runner cut off from the database without a FIN or a RST (a node partition; here `docker network
        disconnect`) keeps its session, and with it the migration lock, until the server finds the peer gone: with
        tcp_keepalives_idle 0, PostgreSQL's default and CloudNativePG's (it sets none), only after the OS's 7200 s
        of idle. The pod that replaces it waits for that lock. migrate.sh's sessions ask for keepalives of their own."""
        postgres, openfga = pinned_images()
        network = f"inkcast-review-374-net-{os.getpid()}"
        self.server, runner = f"{network}-db", f"{network}-runner"
        self.docker("network", "create", network)
        self.addCleanup(self.docker, "network", "rm", network, check=False)
        self.docker("run", "-d", "--rm", "--name", self.server, "--network", network, postgres, "bash", "-c",
                    "initdb -D /tmp/pg -U postgres --auth-local=trust --auth-host=scram-sha-256 >/dev/null"
                    " && echo 'host all all 0.0.0.0/0 scram-sha-256' >> /tmp/pg/pg_hba.conf"
                    " && exec postgres -D /tmp/pg -c listen_addresses='*' -k /tmp")
        self.addCleanup(self.docker, "rm", "-f", self.server, check=False)
        uri = f"postgres://openfga:{PASSWORD}@{self.server}:5432/openfga?sslmode=disable"
        self.docker("run", "-d", "--rm", "--name", runner, "--network", network,
                    "--mount", f"type=image,source={openfga},target=/opt/openfga", "-eOPENFGA_DATASTORE_ENGINE=postgres",
                    f"-eOPENFGA_DATASTORE_URI={uri}", "-eOPENFGA_TIMEOUT=5s", "-eCONNECT_TIMEOUT=30",
                    "--entrypoint", "sleep", postgres, "infinity")
        self.addCleanup(self.docker, "rm", "-f", runner, check=False)
        self.docker("cp", str(SCRIPT), f"{runner}:/tmp/migrate.sh")
        for _ in range(60):
            if self.docker("exec", self.server, "psql", "-h", "/tmp", "-U", "postgres", "-Atc", "SELECT 1", check=False) == "1":
                break
            time.sleep(1)
        self.sql(f"CREATE ROLE openfga LOGIN PASSWORD '{PASSWORD}'", "postgres")
        self.sql("CREATE DATABASE openfga OWNER openfga", "postgres")
        self.docker("exec", runner, "bash", "/tmp/migrate.sh")   # 0 to 6
        # 006 to apply again and held back by a table lock taken from outside: the runner sits in `openfga migrate`,
        # its own session idle with the migration lock.
        self.sql("DELETE FROM goose_db_version WHERE version_id = 6")
        self.sql("DROP INDEX idx_user_lookup")
        self.docker("exec", "-d", self.server, "psql", "-h", "/tmp", "-U", "postgres", "-d", "openfga", "-c",
                    "BEGIN; LOCK TABLE tuple IN SHARE MODE; SELECT pg_sleep(15); COMMIT;")
        time.sleep(1)
        self.docker("exec", "-d", runner, "bash", "/tmp/migrate.sh")
        held = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
        for _ in range(40):
            if self.sql(held) == "1":
                break
            time.sleep(0.25)
        self.assertEqual(self.sql(held), "1", "the runner never took the lock")
        self.docker("network", "disconnect", network, runner)
        cut = time.monotonic()
        while self.sql(held) != "0" and time.monotonic() - cut < 60:
            time.sleep(1)
        self.assertEqual(self.sql(held), "0", f"the lock is still held {time.monotonic() - cut:.0f} s after the runner was cut off")


class NoMigrateJob(unittest.TestCase):
    def test_nothing_acts_on_the_migrate_job_any_more(self):
        """Issue #337's acceptance: nothing in deploy/, scripts/, CI or the docs still waits for or deletes
        openfga-migrate. A check left on a Job the release no longer ships reads whatever an earlier release left on
        the cluster: a Job failed for good there (the K2 state this story removes) fails the check for ever."""
        acting = re.compile(r"\bjobs?[/ ]+openfga-migrate\b")
        found = [f"{path.relative_to(ROOT)}:{n}" for top in ("deploy", "scripts", ".github", "docs") for path in (ROOT / top).rglob("*")
                 if path.is_file() and path.suffix in {".sh", ".py", ".yaml", ".yml", ".md"} and "evidence" not in path.parts
                 for n, line in enumerate(path.read_text().splitlines(), 1) if acting.search(line)]
        self.assertEqual(found, [])
