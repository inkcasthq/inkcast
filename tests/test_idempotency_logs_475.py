# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #475 (466-b, 466-c): what settling an Idempotency-Key reservation logs.

466-b. ``complete_idempotency_key`` wrote the response (in autocommit), then deleted every key older than 24 hours.
When only that cleanup failed (the database busy), its exception reached ``settle_idempotency_key``, which logged
"could not be completed; it expires on its own", although the key was completed and replays. Now the cleanup runs
apart from the UPDATE and its failure is logged as its own.

466-c. The failure lines named the user and fixed strings only, so an operator could not tell which of a person's
reservations stayed pending. Now they name the reservation's ``created_at`` (its owner token, #416), which matches one
row of the person's, and still never the key."""

import functools
import logging
import re
import sqlite3
import time
import unittest

from inkcast.libs.db import Store
from inkcast.services.publishing.rest import settle_idempotency_key

KEY = "k" * 40
LEDGER = "inkcast.libs.db.ledger"
REST = "inkcast.services.publishing.rest"
# Keys long and distinct enough that finding one in a log line can only mean it was logged.
PENDING, OTHER, EXPIRED = "key-pending-" + "p" * 24, "key-other-" + "o" * 24, "key-expired-" + "e" * 24


def logged_created_at(message: str) -> str:
    """The reservation's ``created_at`` as the line gives it, the text an operator pastes into a query."""
    found = re.search(r"created_at ([0-9.]+)", message)
    if found is None:
        raise AssertionError(f"no created_at in {message!r}")
    return found.group(1)


class Base(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", KEY)
        self.addCleanup(self.store.close)
        self.uid = self.store.upsert_user("oidc:https://kc/realms/i#writer", "writer", None, None)["id"]

    def assertNoKey(self, record: logging.LogRecord) -> None:
        text = logging.Formatter().format(record)                          # the message and its traceback
        for key in (PENDING, OTHER, EXPIRED):
            self.assertNotIn(key, text)


class OnlyTheCleanupFails(Base):
    """A reservation completed while the 24-hour cleanup fails: a busy database stood in for by a trigger that refuses
    to delete the one expired row, so the UPDATE runs and only the cleanup's DELETE fails."""

    def setUp(self):
        super().setUp()
        self.store._exec("INSERT INTO idempotency VALUES (?, ?, 'h', '{}', ?)", self.uid, EXPIRED,
                         time.time() - self.store.IDEMPOTENCY_SECONDS - 1)
        self.store._exec(f"CREATE TEMP TRIGGER refuse_cleanup BEFORE DELETE ON idempotency WHEN OLD.key = '{EXPIRED}'"
                         " BEGIN SELECT RAISE(ABORT, 'database is locked'); END")

    def expired_rows(self) -> int:
        return self.store._one("SELECT COUNT(*) AS n FROM idempotency WHERE key = ?", EXPIRED)["n"]

    def test_the_key_is_completed_and_the_cleanup_s_failure_is_logged_as_its_own(self):
        """On main ``complete_idempotency_key`` raised the cleanup's error after its UPDATE had committed."""
        owner = self.store.reserve_idempotency_key(self.uid, PENDING, "h")
        with self.assertLogs(LEDGER, level="WARNING") as logged:
            self.assertTrue(self.store.complete_idempotency_key(self.uid, PENDING, owner, '{"done": 1}'))
        self.assertEqual(self.store.reserve_idempotency_key(self.uid, PENDING, "h"), ("h", '{"done": 1}'), "replays")
        [record] = logged.records
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertIn("older than 24 hours", record.getMessage())
        self.assertIsNotNone(record.exc_info)
        self.assertNoKey(record)
        self.assertEqual(self.expired_rows(), 1, "kept for the next completion")
        self.store._exec("DROP TRIGGER temp.refuse_cleanup")
        other = self.store.reserve_idempotency_key(self.uid, OTHER, "h")
        self.assertTrue(self.store.complete_idempotency_key(self.uid, OTHER, other, "{}"))
        self.assertEqual(self.expired_rows(), 0, "the next completion deletes it")

    def test_settling_says_nothing_of_a_key_that_could_not_be_completed(self):
        """The issue's line: on main ``settle_idempotency_key`` logged "could not be completed; it expires on its own"
        for a key that had been completed."""
        owner = self.store.reserve_idempotency_key(self.uid, PENDING, "h")
        with self.assertLogs("inkcast", level="WARNING") as logged:
            settle_idempotency_key(self.uid, owner, "completed", functools.partial(
                self.store.complete_idempotency_key, self.uid, PENDING, owner, '{"done": 1}'))
        self.assertEqual([r.name for r in logged.records], [LEDGER], [r.getMessage() for r in logged.records])
        self.assertEqual(self.store.reserve_idempotency_key(self.uid, PENDING, "h"), ("h", '{"done": 1}'))


class TheFailureLinesNameTheReservation(Base):
    """Three reservations of one person; the line about one of them leads an operator to that row, and to no other."""

    def setUp(self):
        super().setUp()
        self.owner = self.store.reserve_idempotency_key(self.uid, PENDING, "h")
        self.store.reserve_idempotency_key(self.uid, OTHER, "h")
        self.store._exec("INSERT INTO idempotency VALUES (?, ?, 'h', '{}', ?)", self.uid, EXPIRED, time.time() - 60)

    def matching_keys(self, created_at: str) -> list[str]:
        """The operator's query, with the value pasted from the line as SQL text."""
        rows = self.store._all(f"SELECT key FROM idempotency WHERE user_id = ? AND created_at = {created_at}", self.uid)
        return [r["key"] for r in rows]

    def test_a_write_that_fails_names_its_reservation_and_never_the_key(self):
        """On main the line named the user only."""
        def busy() -> bool:
            raise sqlite3.OperationalError("database is locked")

        for what in ("completed", "released"):
            with self.subTest(what), self.assertLogs(REST, level="ERROR") as logged:
                settle_idempotency_key(self.uid, self.owner, what, busy)
            [record] = logged.records
            self.assertIn(self.uid, record.getMessage())
            self.assertEqual(self.matching_keys(logged_created_at(record.getMessage())), [PENDING])
            self.assertNoKey(record)

    def test_a_reservation_taken_over_is_named_too(self):
        with self.assertLogs(REST, level="WARNING") as logged:
            settle_idempotency_key(self.uid, self.owner, "released", lambda: False)
        [record] = logged.records
        self.assertEqual(float(logged_created_at(record.getMessage())), self.owner)
        self.assertNoKey(record)


if __name__ == "__main__":
    unittest.main()
