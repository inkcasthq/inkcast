# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #94: an erased person's data survives in backups until they age out, and comes back with a restore.

The pages say so, with the configured ``INKCAST_BACKUP_KEEP``; a restore carries the ``user_erasures`` audit rows of
the database it replaces into the restored one; and the app, when it opens the store, erases again everyone whose
hash is in ``user_erasures`` but who has rows (``Store.reapply_erasures``), with no second audit row."""

import asyncio
import pathlib
import re
import sqlite3
import tempfile
import time
import unittest

import httpx2

import test_e2e
from inkcast.libs.config import Config, ConfigError, load_config
from inkcast.libs.db import Store
from inkcast.libs.db.backup import restore_backup, take_backup
from inkcast.libs.db.erasure import USER_TABLES, user_hash
from inkcast.libs.http import pages
from inkcast.libs.secrets import derive_key
from test_e2e import Harness
from test_units import UserErasureTests

KEY = "k" * 40
_AD = test_e2e.AccountDeletion      # its helpers, not its tests (the name is deleted below, so not collected here)


class RestoredDatabase(unittest.TestCase):
    seed, rows = UserErasureTests.seed, staticmethod(UserErasureTests.rows)
    PAYLOAD = UserErasureTests.PAYLOAD

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.db = self.dir / "inkcast.db"
        self.backups = self.dir / "backups"

    def counts(self, store, uid):
        return {table: self.rows(store, table, condition, uid) for table, condition in USER_TABLES}

    def restored_before_the_erasure(self):
        """Ada and Bob seeded, a backup taken, Ada erased, the backup restored: her rows are back, with the audit row
        carried over from the database the restore replaced. Returns (ada, bob, bob's rows, the audit rows)."""
        store = Store(str(self.db), KEY)
        ada, _ = self.seed(store, "ada")
        bob, _ = self.seed(store, "bob")
        bob_rows = self.counts(store, bob)
        take_backup(self.db, self.backups, version="test")                 # before the erasure
        self.assertGreater(sum(store.erase_user(ada).values()), 0)
        audit = store.user_erasures()
        self.assertEqual(len(audit), 1)
        store.close()
        restore_backup(self.backups / sorted(p.name for p in self.backups.glob("*.db"))[-1], self.db,
                       now=time.time() + 3600)
        raw = sqlite3.connect(self.db)
        raw.row_factory = sqlite3.Row
        self.addCleanup(raw.close)
        back = int(raw.execute("SELECT COUNT(*) FROM users WHERE id = ?", (ada,)).fetchone()[0])
        self.assertEqual(back, 1, "the backup predates the erasure: her rows are back")
        carried = [dict(r) for r in raw.execute("SELECT user_hash, erased_at FROM user_erasures")]
        self.assertEqual(carried, [{"user_hash": audit[0]["user_hash"], "erased_at": audit[0]["erased_at"]}],
                         "the audit row of the replaced database was carried into the restored one")
        return ada, bob, bob_rows, audit

    def test_reapply_erasures_erases_her_again_with_no_second_audit_row_and_leaves_bob_alone(self):
        ada, bob, bob_rows, audit = self.restored_before_the_erasure()
        store = Store(str(self.db), KEY)
        self.addCleanup(store.close)
        self.assertGreater(sum(self.counts(store, ada).values()), 0)
        done = store.reapply_erasures()
        self.assertEqual(list(done), [ada])
        self.assertTrue(all(type(n) is int and n > 0 for n in done[ada].values()), done)
        self.assertEqual(set(self.counts(store, ada).values()), {0}, "erased again")
        self.assertEqual(self.counts(store, bob), bob_rows, "bob is untouched")
        self.assertEqual(store.user_erasures(), audit, "the record of the erasure is the one already there: no second row")
        self.assertEqual(store.reapply_erasures(), {}, "nothing left: a second run is a no-op")
        self.assertEqual(store.user_erasures(), audit)

    def test_the_app_erases_her_again_when_it_opens_the_store(self):
        """The hook: ``build_context`` (every service process, before anything serves) runs the re-erasure and logs
        ``identity.erasure.reapplied`` with the id and the count only."""
        from inkcast.libs.runtime import build_context
        ada, bob, bob_rows, audit = self.restored_before_the_erasure()
        config = Config(public_url="http://localhost:8787", secret_key=KEY, database_path=str(self.db), login="password",
                        password="p" * 12)
        with self.assertLogs("inkcast.runtime", level="WARNING") as logs:
            ctx = build_context(config)
        self.addCleanup(ctx.store.close)
        self.addCleanup(lambda: asyncio.run(ctx.http.aclose()))
        self.assertEqual(set(self.counts(ctx.store, ada).values()), {0}, "erased again before the app serves")
        self.assertEqual(self.counts(ctx.store, bob), bob_rows)
        self.assertEqual(ctx.store.user_erasures(), audit, "no second audit row")
        (record,) = [r for r in logs.records if getattr(r, "event", "") == "identity.erasure.reapplied"]
        self.assertEqual(record.user_id, ada)
        self.assertGreater(record.rows, 0)
        self.assertNotIn("inkcast_identity_users_deleted_total{", ctx.metrics.render(), "not counted as a deletion again")

    def test_nothing_to_reapply_is_cheap_and_changes_nothing(self):
        store = Store(str(self.db), KEY)
        self.addCleanup(store.close)
        ada, _ = self.seed(store, "ada")
        before = self.counts(store, ada)
        self.assertEqual(store.reapply_erasures(), {}, "no audit rows: nothing is hashed, nothing is written")
        self.assertEqual(self.counts(store, ada), before)
        self.assertEqual(store.user_erasures(), [])
        # An audit row under another key never matches: the hash is salted by this deployment's secret key.
        store._exec("INSERT INTO user_erasures VALUES (?, ?, ?)", user_hash(derive_key("j" * 40, "erasure"), ada), time.time(), "{}")
        self.assertEqual(store.reapply_erasures(), {})
        self.assertEqual(self.counts(store, ada), before)

    def test_a_restore_over_no_database_or_an_older_schema_carries_nothing_and_still_restores(self):
        store = Store(str(self.db), KEY)
        self.seed(store, "ada")
        take_backup(self.db, self.backups, version="test")
        store.close()
        backup = sorted(self.backups.glob("*.db"))[-1]
        self.db.unlink()                                                    # nothing to carry from
        self.assertIsNone(restore_backup(backup, self.db))
        self.assertEqual(Store(str(self.db), KEY).user_erasures(), [])
        current = sqlite3.connect(self.db)                                  # a current database without the table
        current.execute("DROP TABLE user_erasures")
        current.commit()
        current.close()
        self.assertIsNotNone(restore_backup(backup, self.db, now=time.time() + 3600))
        self.assertEqual(Store(str(self.db), KEY).user_erasures(), [])


class TheSetting(unittest.TestCase):
    BASE = {"INKCAST_PUBLIC_URL": "http://localhost:8787", "INKCAST_SECRET_KEY": KEY, "INKCAST_LOGIN": "password",
            "INKCAST_PASSWORD": "p" * 12, "INKCAST_SECRETS_SOURCE": "env"}

    def test_backup_keep_comes_from_the_environment_and_defaults_to_the_cronjobs_14(self):
        import yaml
        self.assertEqual(load_config(self.BASE).backup_keep, 14)
        self.assertEqual(load_config({**self.BASE, "INKCAST_BACKUP_KEEP": "3"}).backup_keep, 3)
        cronjob = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "deploy" / "kustomize" / "components"
                                  / "backup" / "cronjob.yaml").read_text())
        env = {e["name"]: e["value"] for e in cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(int(env["INKCAST_BACKUP_KEEP"]), Config(public_url="http://localhost:8787", secret_key=KEY,
                                                                 database_path=":memory:").backup_keep)
        with self.assertRaisesRegex(ConfigError, "INKCAST_BACKUP_KEEP"):
            load_config({**self.BASE, "INKCAST_BACKUP_KEEP": "two"})
        with self.assertRaisesRegex(ConfigError, "INKCAST_BACKUP_KEEP"):
            load_config({**self.BASE, "INKCAST_BACKUP_KEEP": "0"}).validate()


class ThePages(unittest.TestCase):
    def test_the_deletion_page_says_backups_age_out_within_the_configured_days(self):
        text = pages.deletion_confirm("p", "c", "Ada", 3).body.decode()
        for said in ("Delete your Inkcast account?", "What goes", "What stays", "articles already published", "coming soon",
                     "Continue", "personal\naccess tokens"):
            self.assertIn(said, text, said)
        self.assertRegex(text, r"backups?\b.{0,160}\bwithin 3 days")
        self.assertNotIn("14 days", text, "the number comes from the setting, not a literal")
        self.assertIn("within 14 days", pages.deletion_confirm("p", "c", "Ada", 14).body.decode())

    def test_the_account_deleted_and_in_progress_pages_say_so_too(self):
        deleted = pages.account_deleted(3).body.decode()
        self.assertIn("Your account is deleted and your data on Inkcast is erased", deleted)
        for said in ("Account deleted", "erased", "stay there", "within 3 days"):
            self.assertIn(said, deleted, said)
        self.assertIn("within 14 days", pages.account_deleted(14).body.decode())
        finishing = pages.deletion_finishing(3).body.decode()
        self.assertIn("Deletion in progress", finishing)
        self.assertIn("within 3 days", finishing)
        self.assertNotIn("Nothing of yours remains", finishing)


class ThePagesServed(Harness):
    """The two pages a person sees, through the real flow, with INKCAST_BACKUP_KEEP=3."""

    extra_config = {**_AD.extra_config, "backup_keep": 3}
    person, call, link = _AD.person, _AD.call, _AD.link
    to_confirm_page, continue_to_keycloak = _AD.to_confirm_page, _AD.continue_to_keycloak
    keycloak_form = staticmethod(_AD.keycloak_form)
    setUp = _AD.setUp

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fakes.kc_base_url = f"{cls.config.public_url}/account/return"

    def test_both_pages_show_the_configured_number_of_days(self):
        ada = self.person("kc-94-ada")
        link = self.link(ada)

        async def run():
            async with httpx2.AsyncClient(follow_redirects=False, timeout=20) as c:
                page = await self.to_confirm_page(c, link)
                confirm = await self.continue_to_keycloak(c, page)
                deleted = await c.post(self.keycloak_form(confirm), data={})
                back = re.search(r'href="([^"]+)"[^>]*>[^<]*Back to Application', deleted.text).group(1)
                return page, await c.get(back)
        page, home = asyncio.run(run())
        self.assertIn("Delete your Inkcast account?", page.text)
        self.assertIn("within 3 days", page.text)
        self.assertIn("Your account is deleted and your data on Inkcast is erased", home.text)
        self.assertIn("within 3 days", home.text)
        self.assertEqual(len(self.app.store.user_erasures()), 1)


del _AD, UserErasureTests                  # helpers only; their tests belong to their own modules

if __name__ == "__main__":
    unittest.main()
