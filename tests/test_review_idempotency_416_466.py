# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #466 (#416): a rollback to the previous release keeps Git sync working.

A rollback deploys the previous digest against the database this release opened (docs/cicd.md, "Rollback"), and the
COLUMNS mechanism counts on old code running beside a new column (ADR-0017: "a rolling deploy, a rollback"). The
previous release reserves an Idempotency-Key with a positional INSERT of the table's five columns
(``INSERT OR IGNORE INTO idempotency VALUES (?, ?, ?, '', ?)``, ledger.py at b504812). A sixth column makes SQLite
refuse it ("table idempotency has 6 columns but 5 values were supplied") on every keyed ``stories:sync``, and the CLI
and the action send a key with every request: Git sync answers 500 until the release is rolled forward again. The
statements below are the previous release's, verbatim, run on a database this release has opened."""

import contextlib
import shutil
import sqlite3
import tempfile
import time
import unittest

from inkcast.libs.db import Store

KEY = "k" * 40
PREVIOUS_RESERVE = "INSERT OR IGNORE INTO idempotency VALUES (?, ?, ?, '', ?)"
PREVIOUS_COMPLETE = "UPDATE idempotency SET response = ? WHERE user_id = ? AND key = ?"
PREVIOUS_RELEASE = "DELETE FROM idempotency WHERE user_id = ? AND key = ? AND response = ''"


class ThePreviousReleaseRunsOnThisReleasesDatabase(unittest.TestCase):
    def test_it_reserves_completes_and_releases_its_keys(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = directory + "/inkcast.db"
        store = Store(path, KEY)                                            # this release opens it, then rolls back
        uid = store.upsert_user("oidc:https://kc/realms/i#writer", "writer", None, None)["id"]
        store.close()
        with contextlib.closing(sqlite3.connect(path)) as c:               # the previous release, statement by statement
            self.assertEqual(c.execute(PREVIOUS_RESERVE, (uid, "k-kept", "h", time.time())).rowcount, 1)
            self.assertEqual(c.execute(PREVIOUS_COMPLETE, ('{"kept": 1}', uid, "k-kept")).rowcount, 1)
            self.assertEqual(c.execute(PREVIOUS_RESERVE, (uid, "k-freed", "h", time.time())).rowcount, 1)
            self.assertEqual(c.execute(PREVIOUS_RELEASE, (uid, "k-freed")).rowcount, 1)
            c.commit()
        store = Store(path, KEY)                                            # and this release again, rolled forward
        self.addCleanup(store.close)
        self.assertEqual(store.reserve_idempotency_key(uid, "k-kept", "h"), ("h", '{"kept": 1}'), "replayed")
        self.assertNotIsInstance(store.reserve_idempotency_key(uid, "k-freed", "h"), tuple, "free: this request holds it")


if __name__ == "__main__":
    unittest.main()
