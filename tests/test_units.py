import asyncio
import time
import unittest
from datetime import datetime, timezone

import httpx2
from fakes import seed_people

from inkcast.auth.provider import InkcastOAuthProvider
from inkcast.config import Config
from inkcast.platforms import PlatformError, discover
from inkcast.platforms.devto import DevtoPlatform
from inkcast.platforms.medium import MediumPlatform
from inkcast.storage import Store
from inkcast.story import StoryError, parse_story

NOW = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)
KNOWN = ["medium", "devto"]
FILE = """---
title: Blog title
description: The summary
tags: [AI, Machine Learning]
canonical_url: https://blog.example.com/post
medium:
  title: Punchier Medium title
  publish_status: unlisted
devto:
  series: Agents
  tags: [ai, ml]
---

# Blog title

![arch](./img/arch.png)

Body.
"""


class StoryTests(unittest.TestCase):
    def test_front_matter_and_overrides(self):
        s = parse_story(FILE, known_platforms=KNOWN, now=NOW)
        self.assertEqual((s.title, s.subtitle), ("Blog title", "The summary"))
        self.assertNotIn("# Blog title", s.body)  # title is presented by each platform
        self.assertEqual(s.for_platform("medium")["title"], "Punchier Medium title")
        self.assertEqual(s.for_platform("devto")["tags"], ["ai", "ml"])
        self.assertTrue(any("./img/arch.png" in w for w in s.warnings))

    def test_image_base_url(self):
        s = parse_story(FILE, known_platforms=KNOWN, image_base_url="https://blog.example.com/post/", now=NOW)
        self.assertIn("https://blog.example.com/post/img/arch.png", s.body)

    def test_schedule_validation(self):
        s = parse_story(FILE, known_platforms=KNOWN, publish_at="2026-10-21T09:00:00-05:00", now=NOW)
        self.assertEqual(s.publish_at.isoformat(), "2026-10-21T14:00:00+00:00")
        with self.assertRaisesRegex(StoryError, "time zone"):
            parse_story(FILE, known_platforms=KNOWN, publish_at="2026-10-21T09:00:00", now=NOW)
        with self.assertRaisesRegex(StoryError, "past"):
            parse_story(FILE, known_platforms=KNOWN, publish_at="2026-01-01T00:00:00Z", now=NOW)

    def test_errors(self):
        with self.assertRaisesRegex(StoryError, "title"):
            parse_story("just text", known_platforms=KNOWN, now=NOW)
        with self.assertRaisesRegex(StoryError, "Unknown platform"):
            parse_story(FILE, known_platforms=KNOWN, platforms=["myspace"], now=NOW)
        with self.assertRaisesRegex(StoryError, "canonical_url"):
            parse_story(FILE, known_platforms=KNOWN, canonical_url="blog/post", now=NOW)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.http = httpx2.AsyncClient()
        self.story = parse_story(FILE, known_platforms=KNOWN, now=NOW)

    def test_medium_prepare(self):
        p = MediumPlatform(self.http).prepare(self.story.for_platform("medium"), self.story.body)
        self.assertTrue(p.payload["content"].startswith("# Punchier Medium title\n\n## The summary\n\n"))
        self.assertEqual(p.payload["publishStatus"], "unlisted")
        self.assertEqual(p.payload["canonicalUrl"], "https://blog.example.com/post")
        with self.assertRaises(PlatformError):
            MediumPlatform(self.http).prepare({**self.story.for_platform("medium"), "title": "x" * 101}, "b")

    def test_devto_prepare(self):
        fields = {**self.story.for_platform("devto"), "tags": ["AI", "Machine Learning", "c++", "go", "rust"]}
        p = DevtoPlatform(self.http).prepare(fields, self.story.body)
        a = p.payload["article"]
        self.assertEqual(a["tags"], ["ai", "machinelearning", "c", "go"])
        self.assertEqual((a["description"], a["series"]), ("The summary", "Agents"))
        self.assertTrue(any("lowercase" in w for w in p.warnings))

    def test_registry_has_builtins(self):
        self.assertEqual(set(discover()), {"medium", "devto"})


class RaceTests(unittest.TestCase):
    """Check-then-act races found in review (2026-10-18): each test forces the interleaving that used to break."""

    def setUp(self):
        self.cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:",
                          login="password", password="p" * 12)
        self.store = Store(self.cfg.database_path, self.cfg.secret_key)
        self.user = self.store.upsert_user("local:owner", "owner", None, None)

    def test_cancel_cannot_race_the_scheduler(self):
        from inkcast.libs.pipeline import Publisher
        post_id = self.store.create_post(group_id="g", user_id=self.user["id"], platform="devto", title="T",
                                         status="scheduled", run_at=time.time() - 1, payload={})
        real_get = self.store.get_post

        def get_then_scheduler_claims(pid):
            row = real_get(pid)
            self.store.get_post = real_get
            self.assertEqual(len(self.store.claim_due_posts(time.time())), 1)   # the scheduler wins the gap
            return row

        self.store.get_post = get_then_scheduler_claims
        with self.assertRaisesRegex(PlatformError, "is publishing"):
            Publisher(self.store, {}).cancel(self.user["id"], post_id)
        self.assertEqual(self.store.get_post(post_id)["status"], "publishing")    # not reported as cancelled

        other = self.store.create_post(group_id="g", user_id=self.user["id"], platform="devto", title="U",
                                       status="scheduled", run_at=time.time() + 3600, payload={})
        self.assertEqual(Publisher(self.store, {}).cancel(self.user["id"], other), "U")
        self.assertFalse(self.store.cancel_if_pending(other, self.user["id"]))  # second cancel: nothing to do
        self.assertFalse(self.store.cancel_if_pending(post_id, "someone-else"))

    def _refresh(self, scopes):
        from mcp.shared.auth import OAuthClientInformationFull
        oauth = InkcastOAuthProvider(self.cfg, self.store)
        client = OAuthClientInformationFull(client_id="c1", redirect_uris=["http://localhost:1/cb"],
                                            token_endpoint_auth_method="none")
        tokens = oauth._issue("c1", self.user["id"], scopes, None, family="f1")
        rt = asyncio.run(oauth.load_refresh_token(client, tokens.refresh_token))
        return oauth, client, rt

    def test_a_refresh_token_can_be_used_once_even_concurrently(self):
        from mcp.server.auth.provider import TokenError
        oauth, client, rt = self._refresh(["posts:read", "posts:write"])

        async def both():
            return await asyncio.gather(oauth.exchange_refresh_token(client, rt, []),
                                        oauth.exchange_refresh_token(client, rt, []), return_exceptions=True)

        results = asyncio.run(both())                     # both loaded the token before either rotated it
        self.assertEqual(sum(isinstance(r, TokenError) for r in results), 1)
        self.assertEqual(len({r["family"] for r in self.store._all("SELECT family FROM tokens")}), 1)

    def test_rotation_is_atomic_across_connections(self):
        import tempfile
        import threading
        path = tempfile.mkdtemp() + "/race.db"
        a, b = Store(path, self.cfg.secret_key), Store(path, self.cfg.secret_key)
        user = a.upsert_user("local:owner", "owner", None, None)
        a.save_token("ic_rt_x", "refresh", "c1", user["id"], ["posts:read"], None, "fam", 600)
        start, won = threading.Barrier(2), []

        def rotate(store):
            start.wait()
            won.append(store.rotate_refresh("ic_rt_x", "c1"))

        threads = [threading.Thread(target=rotate, args=(s,)) for s in (a, b)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(r is not None for r in won), 1)
        self.assertIsNone(a.rotate_refresh("ic_rt_x", "c2"))

    def test_refresh_never_widens_the_requested_scope(self):
        from mcp.server.auth.provider import TokenError
        oauth, client, rt = self._refresh(["posts:read", "posts:write"])
        with self.assertRaises(TokenError) as refused:
            asyncio.run(oauth.exchange_refresh_token(client, rt, ["connections:write"]))
        self.assertEqual(refused.exception.error, "invalid_scope")
        self.assertIsNotNone(self.store.get_token(rt.token, "refresh"))       # refused before anything rotated
        narrowed = asyncio.run(oauth.exchange_refresh_token(client, rt, ["posts:read"]))
        self.assertEqual(narrowed.scope, "posts:read")
        _, _, rt2 = self._refresh(["posts:read", "posts:write"])
        self.assertEqual(asyncio.run(oauth.exchange_refresh_token(client, rt2, [])).scope, "posts:read posts:write")


class UserDisableTests(unittest.TestCase):
    """E9.F9.5.S2: the store side of the Keycloak user sync."""

    def test_an_existing_database_gains_the_disabled_column(self):
        import os
        import sqlite3
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)                      # a users table from before the column existed
        old.execute("CREATE TABLE users (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, display TEXT NOT NULL,"
                    " email TEXT, name TEXT, created_at REAL NOT NULL, last_login_at REAL NOT NULL)")
        old.execute("INSERT INTO users VALUES ('usr_1', 'oidc:x#1', 'ada', NULL, NULL, 1, 1)")
        old.commit()
        old.close()
        store = Store(path, "k" * 40)
        self.assertIsNone(store.find_user_by_identity("oidc:x#1")["disabled_at"])
        Store(path, "k" * 40)                            # opening again is a no-op, not an error

    def test_disabling_cuts_off_everything_at_once_and_signing_in_clears_it(self):
        store = Store(":memory:", "k" * 40)
        ada = store.upsert_user("oidc:https://kc/realms/i#ada", "ada", None, None)
        bob = store.upsert_user("oidc:https://kc/realms/i#bob", "bob", None, None)
        store.save_token("at-ada", "access", "c1", ada["id"], ["posts:read"], None, "f1", 3600)
        store.save_token("at-bob", "access", "c1", bob["id"], ["posts:read"], None, "f2", 3600)
        store.save_code("code-ada", "c1", ada["id"], "{}")
        pat, _ = store.create_api_token(ada["id"], "ci", ["posts:read"], None)

        self.assertEqual(store.disable_user(ada["id"]), {"oauth_tokens": 1, "personal_access_tokens": 1, "codes": 1})
        self.assertIsNone(store.get_token("at-ada", "access"))
        self.assertIsNone(store.get_code("code-ada"))
        self.assertIsNone(store.find_api_token(pat))
        self.assertIsNotNone(store.get_token("at-bob", "access"), "nobody else touched")
        self.assertIsNone(store.disable_user(ada["id"]), "already disabled: nothing done (#102)")
        self.assertEqual([u["id"] for u in store.users_with_identity_prefix("oidc:https://kc/realms/i#")], [bob["id"]])

        # A token minted in the moment of the cut-off is refused while the person stays disabled.
        store.save_token("at-ada-late", "access", "c1", ada["id"], ["posts:read"], None, "f3", 3600)
        self.assertIsNone(store.get_token("at-ada-late", "access"))
        store.upsert_user("oidc:https://kc/realms/i#ada", "ada", None, None)   # Keycloak let them sign in again
        self.assertIsNone(store.find_user_by_identity("oidc:https://kc/realms/i#ada")["disabled_at"])
        self.assertIsNone(store.find_api_token(pat), "a revoked PAT stays revoked")


class UserErasureTests(unittest.TestCase):
    """E3.F3.3.S5 (issue #65): ``Store.erase_user`` removes everything Inkcast holds about one person, at once."""

    PAYLOAD = {"platform": "fake", "title": "T", "payload": {}, "preview": {}}

    def seed(self, store, name):
        """A person with a row in every table ``erase_user`` covers (a new table must be seeded here too)."""
        from inkcast.libs.db.erasure import USER_TABLES
        from inkcast.libs.events import DomainEvent
        from inkcast.libs.kpi.store import Increment
        user = store.upsert_user(f"oidc:https://kc/realms/i#{name}", name, f"{name}@example.com", name.title())
        uid = user["id"]
        store.create_post(group_id="g", user_id=uid, platform="fake", title="T", status="scheduled",
                          run_at=time.time() - 1, payload=self.PAYLOAD)             # + its post.scheduled outbox row
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
        store.record_breaker_trip(time.time(), 1, 1, [(uid, user["last_login_at"])])    # held by the sync's breaker (#105)
        for table, condition in USER_TABLES:
            self.assertGreater(self.rows(store, table, condition, uid), 0, f"seed {table} for the test to mean anything")
        return uid, pat

    @staticmethod
    def rows(store, table, condition, uid):
        with store._lock:
            return int(store._conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {condition}",
                                           {"user_id": uid}).fetchone()["n"])

    def test_everything_about_one_person_goes_and_nothing_about_another(self):
        from inkcast.libs.db.erasure import USER_TABLES
        store = Store(":memory:", "k" * 40)
        ada, ada_pat = self.seed(store, "ada")
        bob, bob_pat = self.seed(store, "bob")
        before = {table: self.rows(store, table, condition, bob) for table, condition in USER_TABLES}

        counts = store.erase_user(ada)
        self.assertEqual(set(counts), {table for table, _ in USER_TABLES})
        self.assertTrue(all(type(n) is int for n in counts.values()), counts)      # ints, never bool or None
        self.assertTrue(all(n > 0 for n in counts.values()), counts)
        self.assertEqual(counts["outbox"], 2, "the post.scheduled and the enqueued event")
        for table, condition in USER_TABLES:
            self.assertEqual(self.rows(store, table, condition, ada), 0, table)
            self.assertEqual(self.rows(store, table, condition, bob), before[table], f"{table}: bob is untouched")
        self.assertIsNone(store.find_user_by_identity("oidc:https://kc/realms/i#ada"))
        self.assertIsNone(store.get_token("at-ada", "access"))
        self.assertIsNone(store.find_api_token(ada_pat))
        self.assertIsNotNone(store.find_api_token(bob_pat))
        self.assertEqual(store.connected_accounts(ada), {})
        self.assertEqual(list(store.connected_accounts(bob)), ["fake"])
        self.assertEqual([r["user_id"] for r in store.claim_due_posts(time.time())], [bob], "only bob's post is due")

    def test_the_pending_deletion_record_outlives_the_erasure(self):
        """The visit back from Keycloak must still find the record when the sync's event pass erased first."""
        store = Store(":memory:", "k" * 40)
        ada, _ = self.seed(store, "ada")
        ticket = store.create_deletion_request(ada, 900)
        req = store.open_deletion_link(ticket)
        cookie = store.arm_deletion_return(req["id"], "kc-ada", 1800)
        store.erase_user(ada)
        self.assertEqual(store.deletion_request_by_cookie(cookie)["user_id"], ada, "kept: user id and hashes only")
        self.assertEqual(set(store.erase_user(ada).values()), {0})
        store.delete_deletion_request(req["id"])
        self.assertIsNone(store.deletion_request_by_cookie(cookie))

    def test_idempotent_unknown_and_already_erased_return_zeros_and_leave_no_record(self):
        store = Store(":memory:", "k" * 40)
        zeros = store.erase_user("usr_nobody")
        self.assertEqual(set(zeros.values()), {0})
        self.assertEqual(store.user_erasures(), [])
        ada, _ = self.seed(store, "ada")
        self.assertGreater(sum(store.erase_user(ada).values()), 0)
        self.assertEqual(set(store.erase_user(ada).values()), {0})
        self.assertEqual(len(store.user_erasures()), 1, "the second run changed nothing, so it is not an erasure")

    def test_the_audit_record_holds_a_salted_hash_the_time_and_counts_and_no_personal_data(self):
        import json
        from inkcast.libs.db.erasure import user_hash
        from inkcast.libs.secrets import derive_key
        store = Store(":memory:", "k" * 40)
        ada, pat = self.seed(store, "ada")
        start = time.time()
        counts = store.erase_user(ada)
        (record,) = store.user_erasures()
        self.assertEqual(record["counts"], counts)
        self.assertGreaterEqual(record["erased_at"], start)
        self.assertEqual(record["user_hash"], user_hash(derive_key("k" * 40, "erasure"), ada))
        self.assertNotEqual(record["user_hash"], user_hash(derive_key("j" * 40, "erasure"), ada), "salted by the key")
        self.assertEqual(len(record["user_hash"]), 64)
        # The row itself, byte for byte, holds nothing that identifies the person.
        columns = [c[1] for c in store._conn.execute("PRAGMA table_info(user_erasures)")]
        self.assertEqual(columns, ["user_hash", "erased_at", "counts"])
        (row,) = store._conn.execute("SELECT * FROM user_erasures").fetchall()
        # The hash is checked exactly above; masked here, since random hex can contain "ada" (issue #83).
        text = json.dumps({**dict(row), "user_hash": "<hash>"})
        for personal in (ada, ada[4:], "ada", "Ada", "ada@example.com", "oidc:https://kc/realms/i#ada", pat, "s3cret"):
            self.assertNotIn(personal, text)
        self.assertNotIn(ada, json.dumps(store.user_erasures()))

    def test_a_token_minted_in_the_instant_of_the_erasure_is_refused(self):
        """A sign-in finishing as the account is erased: its code, OAuth token or PAT belongs to nobody and is refused."""
        store = Store(":memory:", "k" * 40)
        ada, _ = self.seed(store, "ada")
        store.erase_user(ada)
        store.save_code("late-code", "c1", ada, "{}")
        store.save_token("late-at", "access", "c1", ada, ["posts:read"], None, "f9", 3600)
        late_pat, _ = store.create_api_token(ada, "late", ["posts:read"], None)
        self.assertIsNone(store.get_code("late-code"))
        self.assertIsNone(store.get_token("late-at", "access"))
        self.assertIsNone(store.find_api_token(late_pat))
        bob, bob_pat = self.seed(store, "bob")
        self.assertIsNotNone(store.find_api_token(bob_pat), "a person who exists is served")

    def test_a_scheduled_post_can_never_send_after_the_erasure(self):
        """Like RaceTests: whichever side wins the write lock, nothing of the person's is sent afterwards."""
        import asyncio
        import tempfile
        import threading
        import httpx2
        from inkcast.libs.pipeline import Publisher
        from inkcast.libs.platforms import Account, Platform, Prepared, PublishResult

        path = tempfile.mkdtemp() + "/erase.db"
        store, scheduler, observer = (Store(path, "k" * 40) for _ in range(3))   # two processes, one database

        class Fake(Platform):
            id, name, default_api_url, credential_fields = "fake", "Fake", "https://fake.invalid", []
            sent: list = []     # (title, the owner still existed when the platform was called)

            async def verify(self, creds):
                return Account(id="1", name="fake")

            def prepare(self, fields, body):
                return Prepared(platform="fake", title=fields["title"], payload={}, preview={})

            async def publish(self, creds, account, prepared, destination):
                self.sent.append((prepared.title, observer.get_user(account.id) is not None))
                return PublishResult(url="https://fake/new", remote_id="new", status="published")

        fake = Fake(httpx2.AsyncClient())
        publisher = Publisher(scheduler, {"fake": fake})
        ada, _ = self.seed(store, "ada")

        # 1. The scheduler claimed the post a moment before the erasure: the claim is in its hands, but the send
        #    finds no credentials, sends nothing, and writes neither the post nor an event back.
        (claimed,) = scheduler.claim_due_posts(time.time())
        self.assertEqual(store.erase_user(ada)["posts"], 1, "the claimed ('publishing') post goes with the rest")
        asyncio.run(publisher._send(claimed["id"], ada, fake, Prepared.from_dict(self.PAYLOAD), attempts=1, scheduled=True))
        self.assertEqual(fake.sent, [])
        self.assertIsNone(store.get_post(claimed["id"]))
        self.assertEqual(store._all("SELECT 1 FROM outbox WHERE user_id = ?", ada), [])

        # 2. The erasure came first: the scheduler's claim finds nothing of hers, only bob's post.
        bob, _ = self.seed(store, "bob")
        ada, _ = self.seed(store, "ada")
        store.erase_user(ada)
        self.assertEqual([r["user_id"] for r in scheduler.claim_due_posts(time.time())], [bob])
        self.assertEqual(fake.sent, [])

        # 3. Both at once, from two connections: the write lock orders them; either way the end state is the same.
        for _ in range(5):
            ada, _ = self.seed(store, "ada")
            start = threading.Barrier(2)

            def erase():
                start.wait()
                store.erase_user(ada)

            def tick():
                start.wait()
                asyncio.run(publisher.run_due())

            threads = [threading.Thread(target=erase), threading.Thread(target=tick)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(store._all("SELECT 1 FROM posts WHERE user_id = ?", ada), [])
            self.assertEqual(store._all("SELECT 1 FROM outbox WHERE user_id = ?", ada), [])
            self.assertIsNone(store.get_user(ada))
        # A tick that won the lock outright sent her post while she still existed; nothing went out after she was gone.
        self.assertTrue(all(existed for _, existed in fake.sent), fake.sent)
        self.assertEqual(scheduler.claim_due_posts(time.time()), [], "nothing is left in the queue")
        self.assertEqual(store.get_post(store._one("SELECT id FROM posts WHERE user_id = ?", bob)["id"])["status"],
                         "publishing", "bob's claimed post is still the scheduler's; untouched by any of this")


class DeletionRequestTests(unittest.TestCase):
    """E9.F9.5.S4: the store side of Delete my account: a one-time link, then the cookie for the visit back."""

    def test_the_link_is_one_use_bound_to_the_person_and_the_cookie_finds_the_request_until_it_expires(self):
        store = Store(":memory:", "k" * 40)
        ada = store.upsert_user("oidc:https://kc/realms/i#ada", "ada", None, None)
        ticket = store.create_deletion_request(ada["id"], ttl=900)
        from inkcast.libs.secrets import token_hash
        self.assertEqual(store._one("SELECT link_hash FROM deletion_requests")["link_hash"], token_hash(ticket),
                         "only the hash is stored")
        req = store.open_deletion_link(ticket)
        self.assertEqual(req["user_id"], ada["id"])
        self.assertIsNone(store.open_deletion_link(ticket), "a second visit finds nothing")
        self.assertIsNone(store.open_deletion_link("nope"))
        self.assertEqual(store.get_deletion_request(req["id"])["link_hash"], None)

        cookie = store.arm_deletion_return(req["id"], "kc-ada", ttl=1800)
        self.assertEqual(store.deletion_request_by_cookie(cookie)["keycloak_id"], "kc-ada")
        self.assertIsNone(store.deletion_request_by_cookie("x" * 43))
        self.assertNotIn(cookie, [r["cookie_hash"] for r in store._all("SELECT cookie_hash FROM deletion_requests")])
        store._exec("UPDATE deletion_requests SET expires_at = ?", time.time() - 1)
        self.assertIsNone(store.deletion_request_by_cookie(cookie), "expired")
        self.assertIsNone(store.get_deletion_request(req["id"]))
        store.purge_expired()
        self.assertEqual(store._all("SELECT 1 FROM deletion_requests"), [])
        expired = store.create_deletion_request(ada["id"], ttl=-1)
        self.assertIsNone(store.open_deletion_link(expired))


class OAuthTests(unittest.TestCase):
    def test_refresh_rotation_and_single_use_codes(self):
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:",
                     login="password", password="p" * 12)
        store = Store(cfg.database_path, cfg.secret_key)
        oauth = InkcastOAuthProvider(cfg, store)
        user = store.upsert_user("local:owner", "owner", None, None)

        async def run():
            from mcp.server.auth.provider import AuthorizationParams, TokenError
            from mcp.shared.auth import OAuthClientInformationFull
            client = OAuthClientInformationFull(client_id="c1", redirect_uris=["http://localhost:1/cb"],
                                                token_endpoint_auth_method="none")
            await oauth.register_client(client)
            params = AuthorizationParams(state="s", scopes=["inkcast"], code_challenge="x" * 43,
                                         redirect_uri="http://localhost:1/cb", redirect_uri_provided_explicitly=True)
            url = await oauth.authorize(client, params)
            pending = store.get_pending(url.split("pending=")[1])
            store.set_pending_user(pending["id"], user["id"])
            redirect = oauth.approve(store.get_pending(pending["id"]))
            code = redirect.split("code=")[1].split("&")[0]
            ac = await oauth.load_authorization_code(client, code)
            tokens = await oauth.exchange_authorization_code(client, ac)
            with self.assertRaises(TokenError):
                await oauth.exchange_authorization_code(client, ac)
            self.assertEqual((await oauth.load_access_token(tokens.access_token)).subject, user["id"])
            rt = await oauth.load_refresh_token(client, tokens.refresh_token)
            new = await oauth.exchange_refresh_token(client, rt, [])
            self.assertIsNone(await oauth.load_access_token(tokens.access_token))
            self.assertIsNone(await oauth.load_refresh_token(client, tokens.refresh_token))
            self.assertIsNotNone(await oauth.load_access_token(new.access_token))
            # Tokens are stored hashed, credentials encrypted.
            raw = store._all("SELECT token_hash FROM tokens")
            self.assertTrue(all(new.access_token not in r["token_hash"] for r in raw))
            store.save_credentials(user["id"], "devto", {"api_key": "secret-key"}, {"id": "1", "name": "x"})
            sealed = store._one("SELECT sealed FROM credentials")["sealed"]
            self.assertNotIn("secret-key", sealed)
        asyncio.run(run())


class ConfigTests(unittest.TestCase):
    def test_secrets_from_files(self):
        import os
        import tempfile
        from inkcast.config import load_config
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("s" * 40 + "\n")
        env = {"INKCAST_SECRET_KEY_FILE": f.name, "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            cfg = load_config()
            self.assertEqual(cfg.secret_key, "s" * 40)
            cfg.validate()
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class GitHubIdentityTests(unittest.TestCase):
    def _finish(self, orgs, allowed_orgs=(), allowed_users=()):
        from starlette.requests import Request
        from inkcast.auth.identity import GitHubIdentity

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/login/oauth/access_token":
                return httpx2.Response(200, json={"access_token": "gho_x"})
            if request.url.path == "/user":
                return httpx2.Response(200, json={"id": 99, "login": "Ada", "name": "Ada L", "email": None})
            if request.url.path == "/user/orgs":
                return httpx2.Response(200, json=[{"login": o} for o in orgs])
            return httpx2.Response(404)

        cfg = Config(public_url="https://inkcast.example.com", secret_key="k" * 40, database_path=":memory:",
                     login="github", github_client_id="id", github_client_secret="sec",
                     github_allowed_orgs=allowed_orgs, github_allowed_users=allowed_users)
        idp = GitHubIdentity(cfg, httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        req = Request({"type": "http", "method": "GET", "path": "/auth/callback", "headers": [],
                       "query_string": b"code=abc&state=pend1"})
        return asyncio.run(idp.finish(req))

    def test_org_member_allowed(self):
        pending, who = self._finish(["Analytical"], allowed_orgs=("analytical",))
        self.assertEqual((pending, who.key, who.display), ("pend1", "github:99", "@Ada"))

    def test_outsider_refused(self):
        from inkcast.auth.identity import LoginError
        with self.assertRaises(LoginError):
            self._finish(["other"], allowed_orgs=("analytical",))
        pending, who = self._finish([], allowed_users=("ada",))
        self.assertEqual(who.key, "github:99")


if __name__ == "__main__":
    unittest.main()


class PipelineRecoveryTests(unittest.TestCase):
    """A post left 'publishing' by a restart is confirmed with the platform or handed to a person, never re-sent blindly."""

    def setUp(self):
        import asyncio
        import httpx2
        from inkcast.libs.db import Store
        from inkcast.libs.pipeline import STUCK_AFTER, Publisher
        from inkcast.libs.platforms import Account, Platform, PlatformError, Prepared, PublishResult

        class Fake(Platform):
            id, name, default_api_url, credential_fields = "fake", "Fake", "https://fake.invalid", []
            capabilities = frozenset({"find"})
            found = None  # what find_existing answers; "error" raises
            sent: list = []

            async def verify(self, creds):
                return Account(id="1", name="fake")

            def prepare(self, fields, body):
                return Prepared(platform="fake", title=fields["title"], payload={"body": body}, preview={})

            async def publish(self, creds, account, prepared, destination):
                self.sent.append(prepared.title)
                return PublishResult(url="https://fake/new", remote_id="new", status="published")

            async def find_existing(self, creds, account, prepared):
                if self.found == "error":
                    raise PlatformError("down")
                return self.found

        self.asyncio, self.STUCK_AFTER, self.PublishResult = asyncio, STUCK_AFTER, PublishResult
        self.store = Store(":memory:", "k" * 40)
        self.store.upsert_user("u", "u", None, None)
        seed_people(self.store, "usr")          # a row for a person without a users row is refused (#196)
        self.fake = Fake(httpx2.AsyncClient())
        self.store.save_credentials("usr", "fake", {"k": "v"}, {"id": "1", "name": "fake"})
        self.publisher = Publisher(self.store, {"fake": self.fake})

    def stuck(self, title="Interrupted"):
        pid = self.store.create_post(group_id="g", user_id="usr", platform="fake", title=title, status="publishing",
                                     run_at=0, payload={"platform": "fake", "title": title, "payload": {}, "preview": {}})
        self.store.update_post(pid)
        self.store._exec("UPDATE posts SET updated_at = ? WHERE id = ?", time.time() - self.STUCK_AFTER - 1, pid)
        return pid

    def test_confirmed_rescheduled_or_unconfirmed_never_a_blind_retry(self):
        found, missing, unknown, errored = self.stuck(), self.stuck(), self.stuck(), self.stuck()
        self.fake.found = self.PublishResult(url="https://fake/it", remote_id="42", status="published")
        self.asyncio.run(self.publisher.run_due())                       # all four are claimed and resolved together
        self.assertEqual(self.store.get_post(found)["status"], "published")   # it had gone out: recorded, not re-sent
        self.assertEqual(self.store.get_post(found)["result_url"], "https://fake/it")
        self.assertEqual(self.fake.sent, [])                                  # nothing was sent during recovery

        for pid, answer in ((missing, None), (unknown, "skip"), (errored, "error")):
            self.store.update_post(pid, status="publishing")
            self.store._exec("UPDATE posts SET updated_at = ? WHERE id = ?", time.time() - self.STUCK_AFTER - 1, pid)
            if answer == "skip":
                self.fake.capabilities = frozenset()    # a platform that can't be asked (Medium)
            else:
                self.fake.capabilities = frozenset({"find"})
                self.fake.found = answer
            with self.assertLogs("inkcast.libs.pipeline", "WARNING") as logs:
                self.asyncio.run(self.publisher.run_due())
            self.assertIn("interrupted mid-send", logs.output[0])
        self.assertEqual(self.store.get_post(missing)["status"], "published")  # missing → re-queued → sent once now
        self.assertEqual(self.fake.sent, ["Interrupted"])
        self.assertEqual(self.store.get_post(unknown)["status"], "unconfirmed")
        self.assertIn("Check Fake, then resend it", self.store.get_post(unknown)["error"])   # a person decides (#226)
        self.assertEqual(self.store.get_post(errored)["status"], "unconfirmed")
        self.assertEqual(self.store.post_counts().get("recovering"), None)
        self.assertEqual(self.store.claim_stuck(time.time()), [])              # a fresh 'publishing' row is left alone

    def test_queue_lag_is_measured_at_claim_time_and_as_the_oldest_due_age(self):
        from inkcast.libs.observability import Metrics
        from inkcast.libs.runtime import build_metrics
        from inkcast.libs.config import Config
        cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:")
        self.publisher.metrics = build_metrics(self.store, {"fake": self.fake}, cfg)
        self.assertIsInstance(self.publisher.metrics, Metrics)
        now = time.time()
        post_id = self.store.create_post(group_id="g", user_id="usr", platform="fake", title="late", status="scheduled",
                                         run_at=now - 90, payload={"platform": "fake", "title": "late", "payload": {}, "preview": {}})
        # Scheduled an hour ago for 90 s ago: lag counts from the later of a post's time and its creation (ADR-0015).
        self.store._exec("UPDATE posts SET created_at = ? WHERE id = ?", now - 3600, post_id)
        self.assertGreaterEqual(self.store.oldest_due_age(now), 90)           # head-of-line lag before the tick
        self.asyncio.run(self.publisher.run_due(now))
        text = self.publisher.metrics.render()
        self.assertIn('inkcast_publish_lag_seconds_bucket{platform="fake",le="120"} 1', text)   # 90 s late
        self.assertIn('inkcast_publish_lag_seconds_bucket{platform="fake",le="60"} 0', text)
        self.assertIn('inkcast_publish_duration_seconds_count{platform="fake"} 1', text)
        self.assertEqual(self.store.oldest_due_age(time.time()), 0.0)        # the queue is empty again
        self.assertIn("inkcast_posts_oldest_due_age_seconds 0.0", text)


class TokenVerifierTests(unittest.TestCase):
    def test_one_lookup_per_verification_and_pats_get_the_gate_scope(self):
        from inkcast.libs.auth import SCOPE, TokenVerifier
        store = Store(":memory:", "k" * 40)
        user = store.upsert_user("local:owner", "owner", None, None)
        pat, _ = store.create_api_token(user["id"], "ci", ["stories:sync"], None)
        reads = []
        original = store.find_api_token
        store.find_api_token = lambda token: (reads.append(1), original(token))[1]
        v = TokenVerifier(store)
        access = v.access_token(pat)
        self.assertEqual((access.subject, access.scopes, len(reads)), (user["id"], [SCOPE, "stories:sync"], 1))
        self.assertEqual(v.principal(pat).scopes, frozenset({"stories:sync"}))
        self.assertIsNone(v.access_token("ink_pat_nope_nope"))
        self.assertIsNone(v.access_token(""))


class CliSafetyTests(unittest.TestCase):
    def test_token_is_never_sent_over_plain_http_to_a_remote_host(self):
        import contextlib
        import io
        import os
        from inkcast.cli import sync_main
        os.environ["INKCAST_TOKEN_TEST"] = "ink_pat_x_y"
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = sync_main(["--url", "http://inkcast.example.com", "--token-env", "INKCAST_TOKEN_TEST", "nothing/*.md"])
        finally:
            del os.environ["INKCAST_TOKEN_TEST"]
        self.assertEqual(code, 2)
        self.assertIn("plain http", err.getvalue())
