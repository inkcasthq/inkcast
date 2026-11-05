# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""migrate.sh names a lost database session for what it is (issue #510).

``query`` reads psql's answer with ``read -t 60`` and, once the read ended, chose its words with ``kill -0`` on the
coprocess: alive meant "no answer from the database session in 60 s". A lost connection ends the read at once (psql
exits and its output closes: a ``read`` status of 1, where a timeout's is above 128), but a coprocess that has exited
and is not yet reaped is a zombie, and ``kill -0`` succeeds on one (kill(2)). So a session the server ended was logged
as 60 s of silence, two seconds in, when bash had not reaped it yet: CI's kind job, run 41937911393,
``test_a_lock_session_that_dies_alone_ends_the_migration_within_10_s``. A zombie can't be made on demand, so the first
test gives the script a ``kill`` that answers ``-0`` as a zombie does (through BASH_ENV, which bash reads at its start),
and a psql stand-in whose session ends after its first statement. The second keeps the timeout's words: a session that
never answers, and a ``read -t`` that times out at once (status 142, as bash's does) rather than after 60 s."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "kustomize" / "components" / "openfga" / "migrate.sh"

# The check before the session (psql -c 'SELECT 1') answers; the session reads its first statement and ends.
STAND_IN = """#!/bin/sh
for a in "$@"; do [ "$a" = -c ] && { echo 1; exit 0; }; done
IFS= read -r _
exit 2
"""
# A session that reads every statement and answers none, until the script's end closes its input.
SILENT = """#!/bin/sh
for a in "$@"; do [ "$a" = -c ] && { echo 1; exit 0; }; done
while IFS= read -r _; do :; done
"""
# kill -0 succeeds on a process that has exited and is not yet reaped: every coprocess is one, for this script.
ZOMBIE = 'kill() { if [ "$1" = -0 ]; then return 0; fi; builtin kill "$@"; }\n'
# The only `read -t` in the script is the session's; bash's timeout status is 128 + SIGALRM. ("$@", not "$*": the
# script calls read with IFS empty.)
TIMEOUT = 'read() { for a in "$@"; do [ "$a" = -t ] && return 142; done; builtin read "$@"; }\n'


def bash() -> str | None:
    """A bash with coproc and ``wait -n`` (4.3 or later), as the pinned PostgreSQL image's."""
    path = shutil.which("bash")
    if path is None:
        return None
    done = subprocess.run([path, "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'], capture_output=True, text=True)
    major, minor = (int(n) for n in done.stdout.strip().split("."))
    return path if (major, minor) >= (4, 3) else None


@unittest.skipIf(bash() is None, "no bash 4.3 or later on PATH")
class ALostSessionIsNamedAsLost(unittest.TestCase):
    def migrate(self, session: str, bash_env: str) -> str:
        """The script's log, run against a psql stand-in, with ``bash_env`` read before it; its exit status is 1."""
        stubs = pathlib.Path(tempfile.mkdtemp(prefix="migrate-session-"))
        self.addCleanup(shutil.rmtree, stubs, True)
        (stubs / "psql").write_text(session)
        (stubs / "openfga").write_text("#!/bin/sh\nexit 0\n")   # checked for, never reached: the lock comes first
        for stub in ("psql", "openfga"):
            (stubs / stub).chmod(0o755)
        (stubs / "env.sh").write_text(bash_env)
        env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "CONNECT_TIMEOUT": "5",
               "OPENFGA_BINARY": str(stubs / "openfga"), "BASH_ENV": str(stubs / "env.sh"),
               "OPENFGA_DATASTORE_URI": "postgres://openfga:not-a-secret@127.0.0.1:5432/openfga?sslmode=disable"}
        done = subprocess.run([bash(), str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        log = done.stdout + done.stderr
        self.assertEqual(done.returncode, 1, log)
        return log

    def test_a_session_that_ended_is_logged_as_ended_while_its_process_is_not_yet_reaped(self):
        log = self.migrate(STAND_IN, ZOMBIE)
        self.assertIn("the database session ended", log)
        self.assertNotIn("no answer from the database session", log)

    def test_a_session_that_never_answers_is_logged_as_silent(self):
        log = self.migrate(SILENT, TIMEOUT)
        self.assertIn("no answer from the database session in 60 s", log)
        self.assertNotIn("the database session ended", log)

if __name__ == "__main__":
    unittest.main()
