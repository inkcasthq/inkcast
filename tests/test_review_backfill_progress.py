# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #193 (issue #177): the fill's log in split mode, where every pod fills at once.

The runbook says each pod logs a line per batch and one line, ``identity.erasure_hash.backfilled``, when every user
has a hash. A pod whose probe finds rows without a hash, and whose transaction then finds that the other pods have
filled them all (it started during the last batch, or lost the lock to them until the end), logs a batch of nothing
("filled 0 of the N ... in 1 batch") and never the line that the fill is done, though it is and the pod comes up: its
log reads like a start cut short."""

import pathlib
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from inkcast.libs.db import Store
from inkcast.libs.db.erasure import user_hash
from inkcast.libs.secrets import derive_key

from test_backfill_progress import LOGGER, KEY, old_database, progress


class APodTheOthersOutran(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"

    def the_other_pods_fill_every_row(self) -> None:
        conn = sqlite3.connect(self.db, timeout=30)
        salt = derive_key(KEY, "erasure")
        ids = [r[0] for r in conn.execute("SELECT id FROM users WHERE erasure_hash IS NULL")]
        conn.executemany("UPDATE users SET erasure_hash = ? WHERE id = ?", [(user_hash(salt, i), i) for i in ids])
        conn.commit()
        conn.close()

    def test_a_pod_that_finds_nothing_left_in_its_batch_says_the_fill_is_done_and_counts_no_batch(self):
        old_database(self.db, 25)
        real_one = Store._one

        def one(store, sql, *args):
            row = real_one(store, sql, *args)
            if sql.startswith("SELECT COUNT(*)") and "erasure_hash IS NULL" in sql:
                self.the_other_pods_fill_every_row()            # after this pod counted, before its first batch
            return row
        with self.assertLogs(LOGGER, level="INFO") as logs, mock.patch.object(Store, "_one", one), \
                mock.patch.object(Store, "ERASURE_HASH_BATCH", 7):
            Store(str(self.db), KEY).close()
        self.assertEqual(progress(logs.records), [("identity.erasure_hash.backfilled", 0, None, 0)],
                         "no batch of nothing, and the line that every user has a hash")


if __name__ == "__main__":
    unittest.main()
