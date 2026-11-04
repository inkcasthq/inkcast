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

    def migrate_command(self, uri: str = URI, connect_timeout: str = "20") -> list[str]:
        env = {"OPENFGA_DATASTORE_ENGINE": "postgres", "OPENFGA_DATASTORE_URI": uri, "OPENFGA_LOG_FORMAT": "json",
               "OPENFGA_TIMEOUT": "5s", "CONNECT_TIMEOUT": connect_timeout}
        return ["docker", "exec", *[f"-e{k}={v}" for k, v in env.items()], self.container, "bash", "/tmp/migrate.sh"]

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
