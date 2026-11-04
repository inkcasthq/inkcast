# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #112, item 5: an erasure stays one erasure, and a key rotation cannot silently leave people un-erased.

* A second erasure of a person already erased neither counts nor audits again: the Keycloak sync's ``erase_person``
  runs twice on the same row as read, as two passes or two replicas would, and the record, the counter and the log
  all say one (in place since #102; kept as a regression test).
* The record of an erasure carries the hash stored with the person's row (#113), so a rotation of ``INKCAST_SECRET_KEY``
  does not stop a restored row from matching it. The one case left is a row that had no hash in the backup (one from
  before the column) and is filled under the current key: if the key was rotated since the record was made, nothing
  matches. The start-up that fills rows beside existing records warns, with counts only."""

import logging
import pathlib
import re
import shutil
import sqlite3
import tempfile
import unittest

from inkcast.libs.config import Config
from inkcast.libs.db import Store
from inkcast.libs.runtime import open_store
from inkcast.services.identity.keycloak_sync import erase_person

KEY = "k" * 40
PAYLOAD = {"platform": "fake", "title": "T", "payload": {}, "preview": {}}


class Counting:
    """What ``erase_person`` counts."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, str]]] = []

    def inc(self, name: str, amount: float = 1, **labels: str) -> None:
        self.calls.append((name, labels))


class ASecondErasure(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = Store(str(self.dir / "inkcast.db"), KEY)
        self.addCleanup(self.store.close)
        self.metrics = Counting()
        user = self.store.upsert_user("oidc:kc#ada", "Ada", None, None)
        self.store.create_post(group_id="g", user_id=user["id"], platform="fake", title="T", status="scheduled",
                               run_at=4e9, payload=PAYLOAD)
        self.user = self.store.get_user(user["id"])                  # the row as every caller reads it

    def test_neither_counts_nor_audits_again(self):
        with self.assertLogs("inkcast.identity.sync", logging.WARNING) as logs:
            first = erase_person(self.store, self.metrics, self.user, "event")
        self.assertEqual(first["users"], 1)
        self.assertEqual([r.event for r in logs.records], ["identity.user.deleted"])
        self.assertEqual(len(self.store.user_erasures()), 1)
        self.assertEqual(self.metrics.calls, [("inkcast_identity_users_deleted_total", {"via": "event"})])
        # The reconcile, or another replica, decided on the same row before the first erasure landed.
        with self.assertNoLogs("inkcast.identity.sync", logging.WARNING):
            second = erase_person(self.store, self.metrics, self.user, "reconcile")
        self.assertEqual(set(second.values()), {0}, "nothing left to erase")
        self.assertEqual(set(self.store.erase_user(self.user["id"]).values()), {0}, "nor without the row as read")
        self.assertEqual(len(self.store.user_erasures()), 1, "one audit record")
        self.assertEqual(len(self.metrics.calls), 1, "counted once")
        self.assertIsNone(self.store.get_user(self.user["id"]))


class AFillBesideRecords(unittest.TestCase):
    """The start-up that fills ``users.erasure_hash`` for rows that had none (a restore of a backup from before the
    column) while erasure records exist: the fill is under the current key, the records may not be."""

    EVENT = "identity.erasure_hash.filled_with_records"

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = self.dir / "inkcast.db"
        self.config = Config(public_url="http://localhost:8787", secret_key=KEY, database_path=str(self.db))
        store = Store(str(self.db), KEY)
        self.people = [store.upsert_user(f"oidc:kc#{n}", n, None, None)["id"] for n in ("ada", "bob", "cy")]
        self.assertEqual(store.erase_user(self.people[0])["users"], 1)         # one record
        store.close()

    def without_hashes(self) -> None:
        """The rows as a backup from before the column holds them."""
        raw = sqlite3.connect(self.db)
        raw.execute("UPDATE users SET erasure_hash = NULL")
        raw.commit()
        raw.close()

    def warnings(self, logs) -> list[logging.LogRecord]:
        return [r for r in logs.records if getattr(r, "event", "") == self.EVENT]

    def test_warns_with_counts_and_nothing_else(self):
        self.without_hashes()
        with self.assertLogs("inkcast.runtime", logging.WARNING) as logs:
            store = open_store(self.config)
        store.close()
        (warning,) = self.warnings(logs)
        self.assertEqual((warning.rows, warning.records, warning.reapplied), (2, 1, 0))
        message = warning.getMessage()
        self.assertIn("INKCAST_SECRET_KEY", message)
        self.assertNotRegex(message, r"[0-9a-f]{32}", "no hash in the line")
        self.assertNotIn(KEY, message)
        self.assertNotIn("usr_", message, "no person in the line")
        self.assertTrue(re.search(r"\b2\b.*\b1\b", message), message)

    def test_quiet_when_nothing_was_filled_or_there_is_no_record(self):
        with self.assertNoLogs("inkcast.runtime", logging.WARNING):
            open_store(self.config).close()                            # every row has its hash: nothing filled
        raw = sqlite3.connect(self.db)
        raw.execute("DELETE FROM user_erasures")
        raw.execute("UPDATE users SET erasure_hash = NULL")
        raw.commit()
        raw.close()
        with self.assertNoLogs("inkcast.runtime", logging.WARNING):
            open_store(self.config).close()                            # filled, but no record to miss


if __name__ == "__main__":
    unittest.main()
