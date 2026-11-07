# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #533: a note holding "; " is split, so readers miss it.

A post's notes are stored in its ``error`` joined with "; ", and read back split on it (``PostsRepo.notes_of``). A note
whose own text held "; " came back in pieces, none of them the note, so every reader that recognises a note whole missed
it: an update's and a success's ``_is_second_copy``, and the equality checks on ``_edited``. Since #504 a copy's URL or id
is escaped where a note names it (``_named``). The decision on the issue
(https://github.com/inkcasthq/inkcast/issues/533#issuecomment-6660072042) closes the rest:

1. A platform's ``name``, which every note puts in as it is: ``discover()`` refuses a plug-in whose name is empty, longer
   than 40 characters, not printable, or holds ";", and logs why.
2. One encoder beside the one decoder: ``PostsRepo.join_notes``. Every writer that joins notes into a post's ``error``
   goes through it, so the format is one place.

The ``Finding`` classes failed on c17a794d: ``discover()`` took any name, and there was no ``join_notes``. The ``Held``
classes pin what was true already: a copy's URL or id that holds the separator reads back whole (#504), the names the
rule accepts load, and rows already stored read as before."""

import ast
import asyncio
import collections
import pathlib
import re
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx2

import inkcast
import test_publishing_keep_notes_531 as t
from inkcast.libs.db.posts import PostsRepo
from inkcast.libs.pipeline import _both_copies, _edited, _is_second_copy, _lost, _named, _second_copy
from inkcast.libs.platforms import BUILTIN, Platform, PlatformError, PublishResult, discover
from inkcast.libs.platforms.devto import DevtoPlatform
from test_review_publishing_214 import Findable

RULE = '1 to 40 printable characters, without ";"'          # as ``discover()`` words the rule it refuses a name by


def plugin(name: object, **attrs: object) -> type[Platform]:
    """A plug-in class named ``name``, as a third-party package registers it; its id is "odd" unless given."""
    return type("Odd", (Platform,), {"id": "odd", "name": name, "default_api_url": "https://odd.invalid",
                                     "credential_fields": [], **attrs})


def registered(*plugins: tuple[str, type]):
    """The ``inkcast.platforms`` entry points, as ``importlib.metadata`` lists them: (entry point name, what it loads)."""
    return mock.patch("inkcast.libs.platforms.entry_points",
                      return_value=[SimpleNamespace(name=ep, load=lambda cls=cls: cls) for ep, cls in plugins])


# ---- 1. discover() refuses a name Inkcast can't write into a note ---------------------------------------------------

REFUSED = {
    "empty": "",
    "41 characters": "N" * 41,
    "a newline": "Odd\nName",
    "a tab": "Odd\tName",
    "a NUL": "Odd\0Name",
    "a zero-width space": "Odd​Name",
    "a no-break space": "Odd Name",
    "a right-to-left override": "Odd‮Name",
    'the separator "; "': "Odd; Name",
    'a ";"': "Odd;Name",
    'a trailing ";", which a note\'s next word makes "; "': "Odd;",
    "not text (None)": None,
    "not text (a number)": 42,
    "not text (bytes)": b"Odd",
}
ACCEPTED = ("N" * 40, "Hash node", "Ünïcødé 日本語", "Odd-Name_2.0 (beta)", "%3B%20", "Odd, Name: Ltd.")


class FindingDiscoverRefusesANameANoteCannotHold(unittest.TestCase):
    def refused(self, cls: type, entry_point: str = "odd-plugin") -> tuple[dict[str, type], str]:
        """``discover()`` with ``cls`` registered: what it found, and the one error it logged."""
        with registered((entry_point, cls)), self.assertLogs("inkcast.libs.platforms", "ERROR") as logged:
            found = discover()
        (record,) = logged.records
        return found, record.getMessage()

    def test_each_refused_shape_is_skipped_with_an_error_naming_the_entry_point_and_the_rule(self):
        for shape, name in REFUSED.items():
            with self.subTest(shape):
                found, message = self.refused(plugin(name))
                self.assertEqual(set(found), set(BUILTIN), "the plug-in was loaded")
                self.assertIn("'odd-plugin'", message)
                self.assertIn(RULE, message)
                # The name is the plug-in's to choose: never logged, so it can't forge a line of the log.
                self.assertNotIn("\n", message)
                if isinstance(name, str) and name:
                    self.assertNotIn(name, message)

    def test_a_plug_in_with_no_name_at_all_is_refused(self):
        found, message = self.refused(type("Nameless", (Platform,), {"id": "odd"}))
        self.assertEqual(set(found), set(BUILTIN))
        self.assertIn(RULE, message)

    def test_a_refused_plug_in_never_replaces_a_built_in(self):
        """A plug-in that would override DEV (its id) with a name that splits notes: the built-in stays."""
        found, _ = self.refused(plugin("DEV; fork", id="devto"), entry_point="devto-fork")
        self.assertIs(found["devto"], DevtoPlatform)


class HeldDiscoverLoadsANameANoteCanHold(unittest.TestCase):
    def test_the_built_in_names_pass_the_rule(self):
        """DEV and Medium, registered as a plug-in would be: loaded as themselves, with no error."""
        for cls in BUILTIN.values():
            with self.subTest(cls.name), registered((cls.id, cls)), self.assertNoLogs("inkcast.libs.platforms", "ERROR"):
                self.assertIs(discover()[cls.id], cls)

    def test_names_within_the_rule_load(self):
        for name in ACCEPTED:
            cls = plugin(name)
            with self.subTest(name), registered(("odd-plugin", cls)), self.assertNoLogs("inkcast.libs.platforms", "ERROR"):
                self.assertIs(discover()["odd"], cls)


# ---- 2. one encoder beside the one decoder --------------------------------------------------------------------------

SRC = pathlib.Path(inkcast.__file__).parent  # the package these tests imported, so the scan reads the code that runs
# Every use of the notes' separator in src/inkcast that writes no post's notes, by (file, enclosing scope): how many.
# Each words other text, so it stays a plain join, or reads or escapes the separator; none goes through ``join_notes``.
OTHER_JOINS = {
    ("env_secrets.py", "describe"): 1,                                       # a secret's change, described
    ("libs/http/rest.py", "Authed.__call__.endpoint"): 1,                     # a 422's message
    # A lookup's reason, one message: ``recover`` words it into one sentence of the error, Git sync into its result.
    ("libs/pipeline.py", "Publisher.confirm"): 1,
    ("libs/platforms/medium.py", "MediumPlatform.describe_error"): 1,         # Medium's error detail, one PlatformError
    ("services/publishing/mcp_tools.py", "build_mcp.inkcast_create_token"): 1,  # a token link's text
    ("services/publishing/sync.py", "GitSync._stands"): 1,                    # Git sync's messages, in its results
    ("services/publishing/sync.py", "GitSync._one"): 1,
    ("services/publishing/sync.py", "GitSync._create"): 1,
    ("libs/db/posts.py", "PostsRepo.join_notes"): 1,                          # the encoder itself
    ("libs/db/posts.py", "PostsRepo.notes_of"): 1,                            # the decoder
    ("libs/pipeline.py", "_named"): 1,                                        # a copy's URL or id, its "; " escaped
}
# Every writer that joins notes into a post's ``error``, by (file, enclosing scope): its calls of ``join_notes``.
WRITERS = {
    ("libs/db/posts.py", "PostsRepo.update_post"): 1,       # ``keep``: the error written, and the note it keeps
    ("libs/db/posts.py", "PostsRepo.record_success"): 2,    # both copies named, over a copy recorded; the success written
    ("libs/db/posts.py", "PostsRepo.record_uncertain"): 2,  # ``_lost``, over a copy recorded; over a later attempt, with the answer
    ("libs/pipeline.py", "Publisher.update"): 1,            # the notes an update keeps, and its own
    ("libs/pipeline.py", "Publisher.settle_edited"): 1,     # the notes left when the edited-while-out note is settled
}


# A format template with the separator between two fields: "%s; %s", "{}; {}", "{0}; {name}".
TEMPLATE = re.compile(r"(%[-#0-9.]*[a-z]|\{[^{}]*\}); (%[-#0-9.]*[a-z]|\{[^{}]*\})")


def scan() -> tuple[collections.Counter, collections.Counter]:
    """Every use of "; " in src/inkcast, each by (file, enclosing scope): the constant itself, whatever joins with it (a
    ``"; ".join(...)``, ``str.join("; ", ...)``, an f-string's "; " between two fields, a ``+ "; "``, a ``SEP = "; "``),
    and a ``%`` or ``.format`` template holding it between two fields. Then every call of ``join_notes``."""
    joins: collections.Counter = collections.Counter()
    encoded: collections.Counter = collections.Counter()
    for path in sorted(SRC.rglob("*.py")):
        where, scope = path.relative_to(SRC).as_posix(), []

        def visit(node: ast.AST) -> None:
            named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if named:
                scope.append(node.name)
            key = (where, ".".join(scope))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and (node.value == "; "
                                                                                 or TEMPLATE.search(node.value)):
                joins[key] += 1
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join_notes":
                encoded[key] += 1
            for child in ast.iter_child_nodes(node):
                visit(child)
            if named:
                scope.pop()

        visit(ast.parse(path.read_text(encoding="utf-8")))
    return joins, encoded


class FindingOneEncoderBesideTheDecoder(unittest.TestCase):
    def test_join_notes_joins_as_every_writer_joined(self):
        """``"; ".join(notes) or None``, the writers' own expression, for each shape they pass."""
        for notes in ([], [""], ["", ""], ["a"], ["a", "b"], ["a; b", "c"], [t.TRIMMED, "x" * 2048], ("a", "b")):
            with self.subTest(notes=notes):
                self.assertEqual(PostsRepo.join_notes(notes), "; ".join(notes) or None)

    def test_a_stored_error_reads_back_and_joins_again_as_it_was(self):
        """``notes_of`` then ``join_notes`` gives back the error stored, a row written before either included."""
        for error in ("a", "a; b", "a;%20b", "a;b", " ; ", "a; ; b", "; ", "x" * 2048):
            with self.subTest(error=error):
                self.assertEqual(PostsRepo.join_notes(PostsRepo.notes_of({"error": error})), error)

    def test_every_join_with_the_separator_is_the_encoder_or_writes_no_posts_notes(self):
        joins, _ = scan()
        self.assertEqual(dict(joins), OTHER_JOINS,
                         'a use of "; " in src/inkcast: if it writes a post\'s notes, join them with '
                         "PostsRepo.join_notes; if it doesn't, list it in OTHER_JOINS with what it writes")

    def test_every_writer_of_a_posts_notes_joins_them_with_join_notes(self):
        _, encoded = scan()
        self.assertEqual(dict(encoded), WRITERS)


# ---- 3. every note a built-in platform's posts get comes back whole -------------------------------------------------

# A copy's URL and id as a platform (or its plug-in) may answer them: the separator and what it is escaped to, its parts
# and their mirror, text NFC and NFD, nothing at all, 2 KB, and percent-encoding.
HOSTILE = ("; ", ";%20", ";", " ;", "Ünïcødé; 日本語", "café", "café", "", "x" * 2048, "%", "%3B%20", None)
URLS = (None, *(h for h in HOSTILE if h is not None), *(f"https://dev.to/on-call/a{h}b" for h in HOSTILE if h is not None))
# Every copy a success can record: one with neither a URL nor an id is never recorded (``Publisher._deliver``).
COPIES = [(url, remote_id) for url in URLS for remote_id in HOSTILE if url or remote_id]


class FindingEveryNoteOfABuiltInPlatformComesBackWhole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = httpx2.AsyncClient()
        cls.platforms = [platform(cls.http) for platform in BUILTIN.values()]

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.http.aclose())

    @staticmethod
    def notes(platform: Platform):
        """Each note Inkcast writes on ``platform``'s posts, from every builder: (kind, note, the copy it names)."""
        for attempt in (1, 2, 10 ** 6):
            yield "second copy", _second_copy(platform, attempt), None
        yield "second copy", _lost(platform), None
        for n, (url, remote_id) in enumerate(COPIES):
            other = COPIES[(n + 1) % len(COPIES)]
            yield "second copy", _both_copies(platform, _named(url, remote_id), _named(*other), n + 1), None
            yield "edited", _edited(platform, url, remote_id), (url, remote_id)

    def test_beside_a_platforms_note_each_reads_back_whole_and_is_recognised(self):
        checked = 0
        for platform in self.platforms:
            plain, splits = f"{platform.name} trimmed the tags to four.", f"{platform.name} said: a; b."
            for n, (kind, note, copy) in enumerate(self.notes(platform)):
                with self.subTest(platform=platform.name, kind=kind, n=n):
                    for said in (plain, splits):
                        for stored in (PostsRepo.join_notes([said, note]), PostsRepo.join_notes([note, said])):
                            back = PostsRepo.notes_of({"error": stored})
                            self.assertEqual(back.count(note), 1, "the note came back in pieces")
                            if said == plain:
                                self.assertEqual(sorted(back), sorted([said, note]))
                            # Recognised whole, and only the note: never a piece of the platform's own.
                            seconds = [piece for piece in back if _is_second_copy(platform, piece)]
                            self.assertEqual(seconds, [note] if kind == "second copy" else [])
                            if kind == "edited":  # matched by equality, worded again from the row's copy
                                self.assertIn(_edited(platform, *copy), back)
                    checked += 1
        self.assertEqual(checked, len(self.platforms) * (4 + 2 * len(COPIES)))

    def test_a_name_the_rule_refuses_for_its_semicolon_splits_a_note(self):
        """Why the rule refuses ";": these names put the separator into the builders' wording."""
        for name in ("Odd; Name", "Odd;"):
            platform = SimpleNamespace(name=name, capabilities=frozenset())
            notes = (_lost(platform), _second_copy(platform, 2), _both_copies(platform, "id 1", "id 2", 2),
                     _edited(platform, "https://odd/1", "1"))
            split = [note for note in notes if note not in PostsRepo.notes_of({"error": PostsRepo.join_notes(["x", note])})]
            self.assertTrue(split, name)


# ---- the issue's case through the writers, and rows already stored -------------------------------------------------

URL_1, ID_1 = "https://findable/café; b", "1; x"   # a copy's URL (NFD) and id, each holding the separator
URL_9, ID_9 = "https://findable/9; ;%20", "9 ;"
UPDATED = "Findable trimmed the tags of the update."


class Updatable(Findable):
    """Findable that edits posts in place: an update answers with the copy it was asked to edit, and a note of its own."""

    capabilities = frozenset({"find", "update"})

    async def update(self, creds, account, remote_id, prepared, destination):
        return PublishResult(url=None, remote_id=remote_id, status="published", notes=[UPDATED])


def copy(url: str, remote_id: str, *notes: str) -> PublishResult:
    return PublishResult(url=url, remote_id=remote_id, status="published", notes=list(notes))


class HeldACopysURLOrIdHoldingTheSeparator(t.Sends):
    """The issue's acceptance: a copy's URL or id holding "; ", through the pipeline's own writers and readers."""

    def test_an_update_keeps_the_note_naming_both_copies_whole(self):
        post_id = self.post("publishing")
        self.sends(post_id, copy(URL_1, ID_1))
        ((refused, error),) = self.sends(post_id, copy(URL_9, ID_9))
        twice = _both_copies(self.platform, _named(URL_1, ID_1), _named(URL_9, ID_9), 1)
        self.assertEqual((refused.status, error), ("published", twice))
        prepared = self.platform.prepare({"title": "t"}, "Edited.")
        updated = asyncio.run(self.publisher.update(self.user, post_id, Updatable(), prepared, ID_1))
        after = self.store.get_post(post_id)
        self.assertEqual((updated.status, after["result_url"], after["remote_id"]), ("published", URL_1, ID_1))
        self.assertEqual(self.store.notes_of(after), [twice, UPDATED], "the update dropped the note naming both copies")

    def test_a_re_sends_success_recognises_an_earlier_sends_lost_answer(self):
        """The earlier send's uncertain answer holds the separator in the platform's text, then lands over another
        worker's re-send (``record_uncertain``); the re-send's success keeps ``_lost``, as ``record_success`` recognises it."""
        lost = _lost(self.platform)
        for answer, said in ((PlatformError("Findable said: a; b", uncertain=True), ["Findable said: a", "b"]),
                             (RuntimeError("boom; bang"), ["internal error: boom", "bang"])):  # ``_crashed``
            with self.subTest(said=said):
                post_id = self.post("publishing")
                self.store.update_post(post_id, attempts=2)                      # another worker's re-send is out
                ((_, error),) = self.sends(post_id, answer, held=1)
                self.assertEqual(self.store.notes_of({"error": error}), [*said, lost])
                ((outcome, error),) = self.sends(post_id, copy(URL_1, ID_1), held=2)
                self.assertEqual((outcome.status, error, outcome.notes), ("published", lost, [lost]))

    def test_the_edited_note_naming_the_copy_is_settled_by_equality(self):
        post_id = self.post("publishing")
        self.changed(post_id)
        ((outcome, error),) = self.sends(post_id, copy(URL_1, ID_1, t.TRIMMED))
        edited = _edited(self.platform, URL_1, ID_1)
        self.assertEqual(self.store.notes_of({"error": error}), [t.TRIMMED, edited])
        self.assertTrue(self.publisher.settle_edited(post_id, self.platform, holds=lambda row: True))
        self.assertEqual(self.store.get_post(post_id)["error"], t.TRIMMED)


class HeldRowsAlreadyStoredReadAsBefore(t.Sends):
    """No migration: ``notes_of`` is unchanged, so a row stored before this change reads as it did."""

    def test_a_stored_a_b_reads_as_two_notes(self):
        post_id = self.post("published")
        self.store.update_post(post_id, error="a; b")
        self.assertEqual(self.store.notes_of(self.store.get_post(post_id)), ["a", "b"])

    def test_no_notes_read_as_before(self):
        for error, notes in ((None, [""]), ("", [""]), ("a", ["a"]), ("a;b", ["a;b"]), ("a; ; b", ["a", "", "b"])):
            with self.subTest(error=error):
                self.assertEqual(PostsRepo.notes_of({"error": error}), notes)


if __name__ == "__main__":
    unittest.main()
