# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Logger state between tests (issue #159).

``NoBearerLinksInLogs`` failed after some other modules with ``'GET /connect/' not found in ''``: the request was
served, but the access log's ``log.log(INFO, ...)`` was dropped. The logging module remembers each logger's
``isEnabledFor`` answers (``Logger._cache``) and forgets them all in ``setLevel``; the test raised its logger's level
by assigning the ``level`` attribute, which forgets nothing, so a False remembered from an earlier request through
the middleware (any request at all, at the root's default WARNING) stood. Alone, or after a
``setLevel`` anywhere, nothing was remembered and the test passed. Separately, the breaker tests run ``inkcast
identity`` in-process, whose ``setup_logging`` replaces the root logger's handlers and level for the process, and
left them so for every test after.
"""

from __future__ import annotations

import logging
import re
import unittest
from pathlib import Path

import test_deletion_link_redaction
import test_review_identity
import test_review_sync_breaker       # the modules, not their classes: imported classes would be collected here again

ROOT = Path(__file__).resolve().parents[1]
QUIETED = ("httpx2", "httpcore2", "uvicorn.access")      # the loggers setup_logging sets a level on, besides root


def root_state() -> tuple:
    root = logging.getLogger()
    return root.handlers[:], root.level, {name: logging.getLogger(name).level for name in QUIETED}


def restore(state: tuple) -> None:
    root = logging.getLogger()
    root.handlers[:] = state[0]
    root.setLevel(state[1])
    for name, level in state[2].items():
        logging.getLogger(name).setLevel(level)


class TheAccessLogTestsSurviveAnEarlierDroppedRequest(unittest.TestCase):
    """Each ran alone, and in discovery order, and failed after any module whose request through the middleware was
    dropped at WARNING: the state is the stdlib's memo, primed here on purpose."""

    def setUp(self):
        saved, http = root_state(), logging.getLogger("inkcast.http")
        level = http.level
        self.addCleanup(http.setLevel, level)
        self.addCleanup(restore, saved)
        logging.getLogger().setLevel(logging.WARNING)       # the default: an INFO line is dropped
        http.setLevel(logging.NOTSET)
        http.info("an earlier request's access line, dropped, and the answer remembered")
        self.assertFalse(http.isEnabledFor(logging.INFO))
        self.assertIs(http._cache.get(logging.INFO), False, "the stdlib's memo of that answer, which setLevel clears")

    def run_case(self, case: unittest.TestCase) -> None:
        result = unittest.TestResult()
        case.run(result)
        failed = [trace.strip().splitlines()[-1] for _, trace in result.failures + result.errors]
        self.assertEqual(failed, [], f"{case.id()} after an earlier dropped request")
        self.assertEqual(result.testsRun, 1)

    def test_the_connect_ticket_test_still_sees_its_request(self):
        self.run_case(test_review_identity.NoBearerLinksInLogs("test_the_access_log_never_holds_a_connect_ticket"))

    def test_the_deletion_ticket_test_still_sees_its_request(self):
        self.run_case(test_deletion_link_redaction.NoDeletionLinksInLogs("test_the_access_log_never_holds_a_deletion_ticket"))


class NothingWritesALoggersLevelDirectly(unittest.TestCase):
    def test_a_level_is_set_through_setLevel_which_clears_every_loggers_memo(self):
        """Assigning a logger's ``level`` attribute, directly or with ``setattr``, changes the level and leaves every
        remembered ``isEnabledFor`` answer in place; ``setLevel`` is the one call that forgets them."""
        direct = re.compile(r"""\.level\s*=[^=]|setattr\([^,]+,\s*["']level["']""")
        found = []
        for folder in ("src", "tests"):
            for path in sorted((ROOT / folder).rglob("*.py")):
                if "static" in path.parts:
                    continue
                for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if direct.search(line):
                        found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
        self.assertEqual(found, [], "a logger's level is set with setLevel(); a direct write leaves stale isEnabledFor "
                                    "answers behind (issue #159). Not a logger? This test needs to learn to tell.")


class ACliRunInProcessLeavesTheRootLoggerAsItWas(unittest.TestCase):
    def test_the_breaker_tests_restore_what_setup_logging_changed(self):
        """``inkcast identity`` calls ``setup_logging``, which installs the process's root handler and levels; a test
        that runs it in-process puts them back (``fakes.logging_restored``), so no test after it logs into the
        StringIO that run captured, or at the level that run chose."""
        before = root_state()
        self.addCleanup(restore, before)
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(test_review_sync_breaker.OperatorInputInTheAudit).run(result)
        self.assertTrue(result.wasSuccessful(), [trace for _, trace in result.failures + result.errors])
        self.assertGreater(result.testsRun, 0)
        after = root_state()
        self.assertEqual([type(h).__name__ for h in after[0]], [type(h).__name__ for h in before[0]], "root handlers")
        self.assertEqual(after[0], before[0], "the same root handlers, not a copy from that run")
        self.assertEqual((after[1], after[2]), (before[1], before[2]), "root and quieted levels")


if __name__ == "__main__":
    unittest.main()
