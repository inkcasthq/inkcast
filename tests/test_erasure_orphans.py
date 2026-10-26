# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #196: rows written for a person in the instant of their erasure.

A request that authenticated while the person's row was there (``_ACTIVE_USER`` passed) can commit its rows just after
the erasure's transaction: a personal access token, a post with its ``post.scheduled`` event, a credential, a ticket.
Nothing swept them: ``reapply_erasures`` joins on ``users``, every product path erases ``as_read`` (nothing once the row
is gone), and the scheduler claimed the orphan post. Now:

* every table in ``USER_TABLES`` refuses such a write where it happens: a ``BEFORE INSERT`` trigger (and ``BEFORE
  UPDATE`` of the owning column) that ignores a row whose person has no ``users`` row, generated from ``OWNERS`` and
  installed at every open; a process from the previous release is bound by it as soon as any process installs it;
* the scheduler's claims (``claim_due_posts``, ``claim_stuck``) look at ``users`` too;
* housekeeping sweeps the orphans of a database from before the guard, once per database, in batches, with no
  second audit row; nothing is scanned at start-up;
* ``erase_user`` writes its audit row only when it deleted the person's row, so erasing the same person again, by any
  path, adds none.

The interleavings are forced, never timed: two real connections on one file (the API's process and the sync's), and a
hook on the request's lookup that erases the person before the request goes on to write.
"""

from __future__ import annotations

import pathlib
import secrets
import shutil
import sqlite3
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx2

from inkcast.launcher import build, resolve
from inkcast.libs.auth import TokenVerifier
from inkcast.libs.config import Config, RetentionPolicy
from inkcast.libs.db import Store
from inkcast.libs.db.erasure import OWNERS, USER_TABLES, owner_guards, user_hash
from inkcast.libs.events import DomainEvent
from inkcast.libs.http.app import compose
from inkcast.libs.kpi.store import Increment
from inkcast.libs.pipeline import STUCK_AFTER
from inkcast.services.scheduler import housekeeping
from test_e2e import free_port, serve
from test_fable_review_erasure_hash import Traced

KEY = "k" * 40
PAYLOAD = {"platform": "fake", "title": "T", "payload": {}, "preview": {}}
ZEROS = {table: 0 for table, _ in USER_TABLES}


def rows(store: Store, table: str, condition: str, user_id: str) -> int:
    with store._lock:
        return int(store._conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {condition}",
                                       {"user_id": user_id}).fetchone()["n"])


def rows_of(store: Store, user_id: str) -> dict[str, int]:
    """The person's rows per table in ``USER_TABLES``."""
    return {table: rows(store, table, condition, user_id) for table, condition in USER_TABLES}


def seed(store: Store, name: str) -> tuple[str, str]:
    """A person with a row in every table ``erase_user`` covers; (their id, a personal access token of theirs)."""
    user = store.upsert_user(f"oidc:https://kc/realms/i#{name}", name, f"{name}@example.com", name.title())
    uid = user["id"]
    store.create_post(group_id="g", user_id=uid, platform="fake", title="T", status="scheduled",
                      run_at=time.time() - 1, payload=PAYLOAD)                         # + its post.scheduled event
    store.put_entry(uid, "repo", "posts/a.md", "fake", content_hash="h", status="published")
    store.reserve_idempotency_key(uid, "key-1", "rh")
    store.save_credentials(uid, "fake", {"api_key": "s3cret"}, {"id": uid, "name": name})
    store.create_ticket(uid, "fake")
    store.save_token(f"at-{name}", "access", "c1", uid, ["posts:read"], None, f"fam-{name}", 3600)
    store.save_code(f"code-{name}", "c1", uid, "{}")
    store.set_pending_user(store.create_pending("c1", "{}"), uid)
    pat, _ = store.create_api_token(uid, "ci", ["posts:read"], None)
    store.apply(f"evt-{uid}", time.time(), [Increment("posts_published", "user", uid, {"platform": "fake"})])
    store.authz_write([(f"user:{uid}", "owner", "organization:acme")])
    store.enqueue(DomainEvent("post.cancelled", user_id=uid, subject="pst_x", attrs={"platform": "fake"}))
    store.record_breaker_trip(time.time(), 1, 1, [(uid, user["last_login_at"])])
    for table, n in rows_of(store, uid).items():
        assert n > 0, f"seed {table} for the test to mean anything"
    return uid, pat


def write_as_the_request_would(store: Store, user_id: str, other: str) -> dict:
    """Every write a request can make for a person once it has authenticated, as the repositories make them: a
    scheduled post with its event, a PAT, a credential, a connect ticket, ledger and idempotency rows, OAuth tokens and
    codes, a consent, KPI rollups, an authorization tuple, a bare event, and a breaker pass that names them beside
    ``other``. What each gave back, for the caller to check that none treated the refusal as a failure."""
    out: dict = {}
    out["post"] = store.create_post(group_id="g2", user_id=user_id, platform="fake", title="Late", status="scheduled",
                                    run_at=time.time() - 1, payload=PAYLOAD)
    out["pat"], _ = store.create_api_token(user_id, "late", ["posts:read"], None)
    store.save_credentials(user_id, "fake", {"api_key": "late"}, {"id": user_id, "name": "late"})
    out["ticket"] = store.create_ticket(user_id, "fake")
    store.put_entry(user_id, "repo", "posts/late.md", "fake", content_hash="h2", status="published")
    out["claimed"] = store.claim_new(user_id, "repo", "posts/new.md", "fake", "h3")
    out["idempotency"] = store.reserve_idempotency_key(user_id, "key-late", "rh")
    store.save_token("at-late", "access", "c1", user_id, ["posts:read"], None, "fam-late", 3600)
    store.save_code("code-late", "c1", user_id, "{}")
    out["pending"] = store.create_pending("c1", "{}")
    store.set_pending_user(out["pending"], user_id)
    out["applied"] = store.apply("evt-late", time.time(), [
        Increment("posts_published", "user", user_id, {"platform": "fake"}),
        Increment("posts_published", "platform", "fake", {"platform": "fake"})])
    store.authz_write([(f"user:{user_id}", "owner", "organization:late")])
    store.enqueue(DomainEvent("post.cancelled", user_id=user_id, subject="pst_late", attrs={"platform": "fake"}))
    out["pass"] = store.record_breaker_trip(time.time(), 2, 2, [(user_id, 1.0), (other, 1.0)])
    return out


def orphan(store: Store, user_id: str) -> None:
    """What a database from before the guard holds after the race: the person's rows with no ``users`` row, beside
    the one audit row of their erasure. Made by hand, since ``erase_user`` takes every row and the guard now refuses
    the writes."""
    with store._lock:
        store._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        store._conn.execute("INSERT INTO user_erasures VALUES (?, ?, ?)",
                            (user_hash(store._erasure_salt, user_id), time.time(), "{}"))


class TwoConnections(unittest.TestCase):
    """The API's process and the sync's, on one database file."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = str(self.dir / "inkcast.db")
        self.api, self.identity = Store(self.path, KEY), Store(self.path, KEY)
        self.addCleanup(self.api.close)
        self.addCleanup(self.identity.close)


class WritesAfterTheErasure(TwoConnections):
    def test_every_write_by_a_request_that_authenticated_before_the_erasure_is_refused(self):
        bob, bob_pat = seed(self.api, "bob")
        ada, pat = seed(self.api, "ada")
        before = rows_of(self.api, bob)
        user = self.api.get_user(ada)
        principal, _ = TokenVerifier(self.api).lookup(pat)              # the request authenticated: her row was there
        self.assertEqual(principal.user_id, ada)
        self.assertEqual(self.identity.erase_user(ada, as_read=user)["users"], 1)   # then the sync erased her
        self.assertEqual(len(self.identity.user_erasures()), 1)

        made = write_as_the_request_would(self.api, ada, other=bob)      # then the request wrote, as it would have
        self.assertEqual(rows_of(self.api, ada), ZEROS, "none of it exists: refused at the write, not swept later")
        self.assertIsNone(self.api.get_post(made["post"]))
        self.assertIsNone(self.api.find_api_token(made["pat"]))
        self.assertIsNone(self.api.get_ticket(made["ticket"]))
        self.assertIsNone(self.api.get_token("at-late", "access"))
        self.assertIsNone(self.api.get_code("code-late"))
        self.assertIsNone(self.api.get_pending(made["pending"])["user_id"], "the consent never got a person")
        self.assertEqual(self.api.connected_accounts(ada), {})
        self.assertEqual([r["user_id"] for r in self.api.claim_due_posts(time.time())], [bob], "nothing of hers to claim")
        # No caller took the refusal as a failure: the event counts as applied (its platform rollup is kept, hers
        # is not), the idempotency key reads as held by this request, the tuple write raised nothing, and the
        # breaker pass holds bob.
        self.assertTrue(made["applied"])
        self.assertEqual(self.api.read("posts_published", "day", "user", ada, 0, time.time() + 1), [])
        self.assertTrue(self.api.read("posts_published", "day", "platform", "fake", 0, time.time() + 1))
        self.assertIsNone(made["idempotency"])
        self.assertEqual([p["user_id"] for p in self.api.breaker_trip_people(made["pass"])], [bob])
        self.assertEqual(rows_of(self.api, bob), {**before, "sync_breaker_people": before["sync_breaker_people"] + 1})
        self.assertIsNotNone(self.api.find_api_token(bob_pat))
        # One audit row, whatever erases her next: the re-erasure at start-up, or the direct call.
        self.assertEqual(self.identity.reapply_erasures(), {})
        self.assertEqual(set(self.identity.erase_user(ada).values()), {0})
        self.assertEqual(len(self.identity.user_erasures()), 1)

    def test_a_write_that_replaces_or_moves_a_row_to_a_person_without_a_row_is_refused_too(self):
        """``INSERT OR REPLACE`` and an ``UPDATE`` of the owning column (the consent's ``pending_auth.user_id``) are
        the two writes that are not plain inserts; a leftover row from before the guard stays for the sweep, never
        replaced."""
        ada, _ = seed(self.api, "ada")
        orphan(self.api, ada)                                            # a credential left from before the guard
        self.api.save_credentials(ada, "fake", {"api_key": "replaced"}, {"id": ada, "name": "ada"})
        creds, _ = self.api.get_credentials(ada, "fake")
        self.assertEqual(creds["api_key"], "s3cret", "the leftover is untouched; the replacement never landed")
        self.assertEqual(rows(self.api, "credentials", "user_id = :user_id", ada), 1)
        pending = self.api.create_pending("c1", "{}")
        self.api.set_pending_user(pending, ada)
        self.assertIsNone(self.api.get_pending(pending)["user_id"])


class TheScheduler(TwoConnections):
    def test_claims_nothing_of_a_person_who_has_no_row(self):
        bob, _ = seed(self.api, "bob")
        ada, _ = seed(self.api, "ada")
        stuck = self.api.create_post(group_id="g", user_id=ada, platform="fake", title="Stuck", status="publishing",
                                     run_at=time.time() - 1, payload=PAYLOAD)
        self.api._exec("UPDATE posts SET updated_at = ? WHERE id = ?", time.time() - 2 * STUCK_AFTER, stuck)
        orphan(self.api, ada)                                            # from before the guard
        self.assertEqual([r["user_id"] for r in self.identity.claim_due_posts(time.time())], [bob])
        self.assertEqual(self.identity.claim_stuck(time.time() - STUCK_AFTER), [], "nor one left 'publishing'")
        statuses = {r["status"] for r in self.api._all("SELECT status FROM posts WHERE user_id = ?", ada)}
        self.assertEqual(statuses, {"scheduled", "publishing"}, "left as they were, for the sweep")


class TheSweep(TwoConnections):
    def test_orphans_from_before_the_guard_are_swept_in_housekeeping_once_with_no_second_audit_row(self):
        bob, bob_pat = seed(self.api, "bob")
        ada, _ = seed(self.api, "ada")
        orphan(self.api, ada)
        before, orphans = rows_of(self.api, bob), rows_of(self.api, ada)
        self.assertTrue(all(n > 0 for table, n in orphans.items() if table != "users"), orphans)

        # A start-up (every process opens the store) leaves them: no scan of a user-keyed table there.
        with Traced() as traced:
            opened = Store(self.path, KEY)
        self.addCleanup(opened.close)
        self.assertEqual([s for s in traced.statements if s.startswith(("DELETE", "SELECT rowid FROM"))], [],
                         "no scan for orphans at start-up, and nothing removed")
        self.assertEqual(opened.reapply_erasures(), {}, "no users row, so no hash to match: not its job")
        self.assertEqual(rows_of(self.api, ada), orphans)

        # Housekeeping, in the scheduler, sweeps them, and says how many with ints only.
        ctx = types.SimpleNamespace(store=opened, config=types.SimpleNamespace(retention=RetentionPolicy()))
        with self.assertLogs("inkcast.services.scheduler", level="WARNING") as logs:
            housekeeping(ctx)
        self.assertEqual(rows_of(self.api, ada), ZEROS)
        self.assertEqual(rows_of(self.api, bob), before, "a living person's rows are untouched")
        self.assertIsNotNone(self.api.find_api_token(bob_pat))
        self.assertEqual(len(opened.user_erasures()), 1, "the record of the erasure is the one already there")
        (record,) = [r for r in logs.records if getattr(r, "event", "") == "identity.erasure.orphans_swept"]
        self.assertEqual(record.rows, sum(n for table, n in orphans.items() if table != "users"))
        self.assertIsInstance(record.tables, int)
        self.assertNotIn(ada, record.getMessage())

        # Once per database: the second run reads the marker and nothing else. The marker lives in the database, so
        # a restore of a backup from before the sweep is swept again, and clearing it runs the sweep once more.
        statements: list[str] = []
        opened._conn.set_trace_callback(statements.append)
        self.assertEqual(opened.sweep_orphans(), {})
        opened._conn.set_trace_callback(None)
        self.assertEqual(len(statements), 1, statements)
        self.assertNotIn("NOT EXISTS", statements[0])
        opened._exec("DELETE FROM sync_state WHERE name = ?", "erasure.orphans_swept")
        self.assertEqual(opened.sweep_orphans(), {})
        self.assertIsNotNone(opened.get_sync_state("erasure.orphans_swept"))
        # Erasing her again, by any path, adds nothing.
        self.assertEqual(set(opened.erase_user(ada).values()), {0})
        self.assertEqual(len(opened.user_erasures()), 1)

    def test_the_sweep_takes_the_rows_in_batches_and_a_run_cut_short_leaves_the_rest_to_the_next(self):
        people = [seed(self.api, f"p{i}")[0] for i in range(5)]
        for uid in people[:4]:
            orphan(self.api, uid)
        # Cut short in its second transaction: what the first deleted stays deleted, and the marker is not set.
        real_tx, calls = Store._tx, [0]

        def cut_short(store):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("the pod was stopped mid-sweep")
            return real_tx(store)
        with mock.patch.object(Store, "ORPHAN_BATCH", 3), mock.patch.object(Store, "_tx", cut_short), \
                self.assertRaises(RuntimeError):
            self.api.sweep_orphans()
        self.assertEqual(sum(rows_of(self.api, uid)["posts"] for uid in people[:4]), 1, "one batch of three went")
        self.assertIsNone(self.api.get_sync_state("erasure.orphans_swept"))
        # The next run finishes: each batch its own write transaction of at most the batch size.
        statements: list[str] = []
        with mock.patch.object(Store, "ORPHAN_BATCH", 3):
            self.api._conn.set_trace_callback(statements.append)
            swept = self.api.sweep_orphans()
            self.api._conn.set_trace_callback(None)
        self.assertEqual(swept["posts"], 1)
        self.assertEqual(swept["outbox"], 8, "the post.scheduled event and the bare one of each")
        self.assertEqual(len([s for s in statements if s.startswith("DELETE FROM outbox")]), 3,
                         "8 rows in batches of 3: three transactions, not one")
        begins = [s for s in statements if s.startswith("BEGIN")]
        self.assertEqual(len(begins), len([s for s in statements if s.startswith("DELETE")]), "one transaction per batch")
        self.assertTrue(all(s == "BEGIN IMMEDIATE" for s in begins), begins)
        self.assertEqual(rows_of(self.api, people[4])["outbox"], 2, "the living person keeps hers")
        self.assertEqual(sum(rows_of(self.api, uid)["outbox"] for uid in people[:4]), 0)
        self.assertIsNotNone(self.api.get_sync_state("erasure.orphans_swept"))

    def test_a_person_whose_row_is_gone_is_erased_without_a_second_audit_row(self):
        """``erase_user(user_id)`` with no ``as_read`` is the one call that still finds the leftovers: it takes them and
        records nothing, since the audit row of the erasure that removed the person's row is already there."""
        ada, _ = seed(self.api, "ada")
        orphan(self.api, ada)
        counts = self.api.erase_user(ada)
        self.assertGreater(sum(counts.values()), 0)
        self.assertEqual(counts["users"], 0)
        self.assertEqual(rows_of(self.api, ada), ZEROS)
        self.assertEqual(len(self.api.user_erasures()), 1)
        # And a living person is erased as before: the rows, and one audit row.
        bob, _ = seed(self.api, "bob")
        self.assertEqual(self.api.erase_user(bob, as_read=self.api.get_user(bob))["users"], 1)
        self.assertEqual(len(self.api.user_erasures()), 2)
        self.assertEqual(set(self.api.erase_user(bob).values()), {0})
        self.assertEqual(len(self.api.user_erasures()), 2)


class TheGuard(unittest.TestCase):
    def test_every_table_in_user_tables_has_its_guard_generated_from_owners(self):
        self.assertEqual({table for table, *_ in OWNERS}, set(dict(USER_TABLES)) - {"users"},
                         "a table erase_user covers is a table the guard covers")
        store = Store(":memory:", KEY)
        self.addCleanup(store.close)
        have = {r["name"]: r["sql"] for r in store._all("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
        wanted = dict(owner_guards())
        self.assertEqual(set(have), set(wanted), "every trigger in the database is a guard, and every guard is there")
        for name, sql in wanted.items():
            self.assertEqual(have[name], sql.replace("IF NOT EXISTS ", "", 1))
            self.assertIn("RAISE(IGNORE)", sql, "ignored, never aborted: a caller must not take it for a failure")
            self.assertTrue(name.startswith("owner_guard_"), name)
        for table, column, *_ in OWNERS:
            self.assertTrue(any(f"BEFORE INSERT ON {table} " in s for s in wanted.values()), table)
            self.assertTrue(any(f"BEFORE UPDATE OF {column} ON {table} " in s for s in wanted.values()), table)
        plan = " ".join(r[3] for r in store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM users WHERE id = ?", ("x",)))
        self.assertNotIn("SCAN", plan, "the guard's probe is a primary-key lookup")

    def test_a_database_from_before_the_guard_gains_it_and_a_later_open_writes_nothing(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "inkcast.db"
        self.addCleanup(shutil.rmtree, path.parent, True)
        Store(str(path), KEY).close()
        wanted = dict(owner_guards())
        conn = sqlite3.connect(path)
        missing = sorted(wanted)[0]
        conn.execute(f"DROP TRIGGER {missing}")                          # as a database from before the guard lacks it
        conn.execute("CREATE TRIGGER owner_guard_posts_insert_00000000 BEFORE INSERT ON posts FOR EACH ROW WHEN 0 "
                     "BEGIN SELECT RAISE(IGNORE); END")                  # a guard from an earlier shape of the body
        conn.commit()
        conn.close()
        with Traced() as traced:
            Store(str(path), KEY).close()
        self.assertEqual([s[:14] for s in traced.statements if s.startswith("CREATE TRIGGER")], ["CREATE TRIGGER"])
        self.assertEqual([s for s in traced.statements if s.startswith("DROP TRIGGER")],
                         ["DROP TRIGGER IF EXISTS owner_guard_posts_insert_00000000"])
        creates = [s for s in traced.statements if s.startswith("CREATE TRIGGER")]
        drops = [s for s in traced.statements if s.startswith("DROP TRIGGER")]
        self.assertLess(traced.statements.index(creates[0]), traced.statements.index(drops[0]),
                        "the wanted guard is in place before a stale one goes: never a moment without one")
        self.assertEqual([s for s in traced.statements if s.startswith("BEGIN")], [], "autocommit statements, each atomic")
        conn = sqlite3.connect(path)
        have = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
        conn.close()
        self.assertEqual(set(have), set(wanted))
        with Traced() as traced:
            Store(str(path), KEY).close()
        self.assertEqual([s for s in traced.statements if s.startswith(("CREATE TRIGGER", "DROP", "BEGIN"))], [],
                         "nothing to do: no schema write")
        self.assertEqual(len([s for s in traced.statements if "sqlite_master" in s and "trigger" in s]), 1, "one read")

    def test_a_process_from_the_previous_release_is_bound_once_any_process_installs_the_guard(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "inkcast.db"
        self.addCleanup(shutil.rmtree, path.parent, True)
        store = Store(str(path), KEY)
        ada, _ = seed(store, "ada")
        bob, _ = seed(store, "bob")
        store.close()
        pre = sqlite3.connect(path)                                      # the database as the old release left it
        for (name,) in pre.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            pre.execute(f"DROP TRIGGER {name}")
        pre.commit()
        pre.close()
        old = sqlite3.connect(path, isolation_level=None, timeout=30)   # a pod on the previous image, mid-request
        insert = ("INSERT INTO connect_tickets VALUES (?, ?, 'fake', ?)")
        self.assertEqual(old.execute(insert, ("t-ada-1", ada, time.time() + 60)).rowcount, 1, "unguarded: it lands")
        old.execute("DELETE FROM users WHERE id = ?", (ada,))           # erased (by hand: the leftovers must stay)
        upgraded = Store(str(path), KEY)                                 # the first pod on the new image opens the store
        self.addCleanup(upgraded.close)
        self.assertEqual(old.execute(insert, ("t-ada-2", ada, time.time() + 60)).rowcount, 0,
                         "the same statement from the old pod is refused now: the guard lives in the database")
        self.assertEqual(old.execute(insert, ("t-bob-2", bob, time.time() + 60)).rowcount, 1)
        self.assertEqual(old.execute("SELECT COUNT(*) FROM connect_tickets WHERE user_id = ?", (ada,)).fetchone()[0], 2,
                         "the one from before the guard, and the seed's; for the sweep")
        old.close()


SERVICES = "IdentityService,PublishingApi"


class ThroughTheStack(unittest.TestCase):
    """The two paths the issue names, REST and MCP, authenticated before the erasure and writing after it: a hook on
    the write erases the person over the sync's own connection, in the instant between the two."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.path = f"{cls.dir}/inkcast.db"
        port = free_port()
        cls.cfg = Config(public_url=f"http://127.0.0.1:{port}", port=port, secret_key="k" * 48, database_path=cls.path,
                         login="password", password="p" * 12)
        cls.cfg.validate()
        specs = resolve(SERVICES)
        cls.ctx = build(cls.cfg, specs)
        cls.server = serve(compose(specs, cls.ctx), port)
        cls.other = Store(cls.path, "k" * 48)                            # the sync's connection, in another process

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.other.close()
        cls.ctx.store.close()
        shutil.rmtree(cls.dir, True)

    def person(self, name: str) -> tuple[str, str]:
        """(a user id, an OAuth access token of theirs), with a platform connected."""
        user = self.ctx.store.upsert_user(f"oidc:x#{name}", name, None, None)["id"]
        access = "ic_at_" + secrets.token_urlsafe(32)
        self.ctx.store.save_token(access, "access", "c1", user, ["inkcast"], None, f"fam-{name}", 3600)
        self.ctx.store.save_credentials(user, "devto", {"api_key": "k"}, {"id": "1", "name": name})
        return user, access

    def erase_before(self, method: str, user_id: str) -> list:
        """The first time the request reaches ``Store.method`` (its write), the person is erased over the other
        connection first, as the sync's pass would land between the request's lookups (the bearer middleware's and
        ``Authed``'s, both of which found them) and its write. The counts the erasure got."""
        real = getattr(self.ctx.store, method)
        row, done = self.ctx.store.get_user(user_id), []

        def hooked(*args, **kwargs):
            if not done:
                done.append(self.other.erase_user(user_id, as_read=row))
            return real(*args, **kwargs)
        patcher = mock.patch.object(self.ctx.store, method, hooked)
        patcher.start()
        self.addCleanup(patcher.stop)
        return done

    def bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def tool(self, access: str, name: str, args: dict) -> tuple[bool, str]:
        import asyncio
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def run():
            got = []
            try:
                async with httpx2.AsyncClient(timeout=30, headers=self.bearer(access)) as http:
                    async with streamable_http_client(self.cfg.mcp_url, http_client=http) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            await session.list_tools()      # cached: the call's validation needs no request after it
                            got.append(await session.call_tool(name, args))
            except Exception:
                if not got:                     # the call itself failed; the session's own close is refused (401)
                    raise                       # once the person is gone, whatever the call did, and that is fine
            (r,) = got
            return r.is_error, "\n".join(c.text for c in r.content if hasattr(c, "text"))
        return asyncio.run(run())

    def test_a_token_asked_for_by_a_request_authenticated_before_the_erasure_never_exists(self):
        user, access = self.person("ada")
        records = len(self.other.user_erasures())
        done = self.erase_before("create_api_token", user)
        r = httpx2.post(f"{self.cfg.public_url}/api/v1/tokens", json={"name": "ci", "scopes": ["posts:read"]},
                        headers=self.bearer(access), timeout=30)
        self.assertEqual([c["users"] for c in done], [1], "the erasure landed between the lookups and the write")
        self.assertEqual(r.status_code, 200, r.text)                     # the write was ignored, not a failure
        self.assertEqual(rows_of(self.ctx.store, user), ZEROS)
        refused = httpx2.get(f"{self.cfg.public_url}/api/v1/posts", headers=self.bearer(r.json()["token"]), timeout=30)
        self.assertEqual(refused.status_code, 401, "the token in the reply belongs to nobody")
        self.assertEqual(len(self.other.user_erasures()), records + 1)

    def test_a_post_scheduled_by_a_request_authenticated_before_the_erasure_never_exists(self):
        at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
        story = {"markdown": "# Later\n\nA story.", "platforms": ["devto"], "publish_at": at}
        for via in ("rest", "mcp"):
            with self.subTest(via=via):
                user, access = self.person(f"ada-{via}")
                done = self.erase_before("create_post", user)        # the publisher read her credentials, then...
                if via == "rest":
                    r = httpx2.post(f"{self.cfg.public_url}/api/v1/posts", json=story, headers=self.bearer(access), timeout=30)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual([x["status"] for x in r.json()["results"]], ["scheduled"])
                else:
                    error, text = self.tool(access, "inkcast_publish", story)
                    self.assertFalse(error, text)
                self.assertEqual([c["users"] for c in done], [1], "the erasure landed between the read and the write")
                self.assertEqual(rows_of(self.ctx.store, user), ZEROS, "no post, no event: nothing for the scheduler")
                self.assertEqual(self.ctx.store.claim_due_posts(time.time() + 2 * 86400), [])
                self.assertEqual(self.ctx.store.user_erasures()[-1]["counts"]["users"], 1)


if __name__ == "__main__":
    unittest.main()
