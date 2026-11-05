# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of #562 (issue #533). Each ``Finding`` class failed on 82c6967c.

1. ``discover()`` checks a plug-in's ``name`` on its class, but every note reads it on the instance (``platform.name``),
   which ``load_platforms`` makes: a name set in ``__init__`` reached the notes with a ";" or a newline in it.
2. ``update_post(keep=)`` asks whether the row's error holds the note with SQL ``instr``, a substring match, where every
   other reader asks ``notes_of``: the ``_lost`` note's text quoted inside a longer note was kept as a note of its own."""

import asyncio
import itertools
import unittest

import httpx2

from inkcast.libs.db.posts import PostsRepo
from inkcast.libs.pipeline import _is_second_copy, _lost
from inkcast.libs.platforms import Platform, load_platforms
from inkcast.libs.platforms.devto import DevtoPlatform
from test_note_boundaries_533 import RULE, registered
from test_review_publishing_214 import Harness


def named_in_init(name: str, **attrs: object) -> type[Platform]:
    """A plug-in whose class is named "Good", the name ``discover()`` reads, and whose ``__init__`` names the instance
    ``name``: the name every note on its posts holds. Its id is "odd" unless given."""

    def __init__(self, http, api_url=None):
        Platform.__init__(self, http, api_url)
        self.name = name

    async def verify(self, creds):
        raise NotImplementedError

    def prepare(self, fields, body):
        raise NotImplementedError

    async def publish(self, creds, account, prepared, destination):
        raise NotImplementedError

    return type("Odd", (Platform,), {"id": "odd", "name": "Good", "default_api_url": "https://odd.invalid",
                                     "credential_fields": [], "__init__": __init__, "verify": verify,
                                     "prepare": prepare, "publish": publish, **attrs})


# ---- 1. the name rule holds for the name the notes read ----------------------------------------------------------------


class FindingTheNameRuleHoldsForTheInstancesName(unittest.TestCase):
    def setUp(self):
        self.http = httpx2.AsyncClient()
        self.addCleanup(lambda: asyncio.run(self.http.aclose()))

    def loaded(self, cls: type, entry_point: str = "odd-plugin") -> tuple[dict[str, Platform], str]:
        """``load_platforms()`` with ``cls`` registered: what it made, and the one error it logged."""
        with registered((entry_point, cls)), self.assertLogs("inkcast.libs.platforms", "ERROR") as logged:
            platforms = load_platforms(self.http)
        (record,) = logged.records
        return platforms, record.getMessage()

    def test_a_name_set_in_init_that_the_rule_refuses_is_not_loaded(self):
        for name in ("Odd; Name", "Odd;", "Odd\nName", ""):
            with self.subTest(name=name):
                platforms, message = self.loaded(named_in_init(name))
                self.assertNotIn("odd", platforms, "the plug-in was loaded")
                self.assertIn(RULE, message)
                self.assertNotIn("\n", message)

    def test_a_built_in_overridden_by_such_a_plug_in_stays(self):
        platforms, _ = self.loaded(named_in_init("DEV; fork", id="devto"), entry_point="devto-fork")
        self.assertIs(type(platforms["devto"]), DevtoPlatform)

    def test_a_name_set_in_init_within_the_rule_loads_and_its_notes_read_back_whole(self):
        with registered(("odd-plugin", named_in_init("Hash node"))), \
                self.assertNoLogs("inkcast.libs.platforms", "ERROR"):
            platform = load_platforms(self.http)["odd"]
        self.assertEqual(platform.name, "Hash node")
        self.assertTrue(_is_second_copy(platform, _lost(platform)))


# ---- 2. update_post(keep=) keeps a note the row holds whole -------------------------------------------------------------


class FindingKeepAsksWhatNotesOfReads(Harness):
    def kept(self, error: str | None, keep: str) -> bool:
        """A lookup's answer over an 'unconfirmed' post whose error is ``error`` (``recover``, ``GitSync``'s check): whether
        the post keeps ``keep``."""
        post_id = self.post("unconfirmed")
        self.store.update_post(post_id, error=error)
        self.store.update_post(post_id, status="published", result_url="https://findable/1", remote_id="1", error=None,
                               keep=keep, only_if="unconfirmed")
        return self.store.get_post(post_id)["error"] == keep

    def test_the_lost_notes_text_inside_a_longer_note_is_not_kept(self):
        lost = _lost(self.platform)
        self.assertFalse(self.kept(f"Findable said: {lost} (quoted)", lost))
        self.assertFalse(self.kept(f"Inkcast restarted, and {lost}. Check Findable.", lost))

    def test_the_lost_note_whole_is_kept(self):
        lost = _lost(self.platform)
        for error in (lost, PostsRepo.join_notes(["Findable said: a; b", lost]), PostsRepo.join_notes([lost, "x"])):
            with self.subTest(error=error):
                self.assertTrue(self.kept(error, lost))

    def test_kept_exactly_when_notes_of_holds_it(self):
        """Every error up to four pieces of "K", ";", " ", "x", and None: kept exactly when ``notes_of`` holds ``keep``.
        A ``keep`` without "; ", as every note Inkcast keeps is (``_lost``: the name rule, #533)."""
        errors = [None, *("".join(p) for n in range(5) for p in itertools.product(("K", ";", " ", "x"), repeat=n))]
        for keep in ("K", "K;K", "K K"):
            for error in errors:
                with self.subTest(keep=keep, error=error):
                    self.assertEqual(self.kept(error, keep), keep in PostsRepo.notes_of({"error": error}))


if __name__ == "__main__":
    unittest.main()
