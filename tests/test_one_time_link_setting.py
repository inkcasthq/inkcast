# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""F1.10.S4 (issue #92): the one-time link lifetime is a setting.

``INKCAST_ONE_TIME_LINK_MINUTES`` (15, 5-60) is how long a connect link (``/connect/<ticket>``, where a platform key is
typed) and an account-deletion link (``/account/delete/<ticket>``) last: one value for both. 60 is a hard maximum, not
a tuning knob: a connect link is a bearer capability that passes through the AI transcript, and whoever holds it can
attach their own platform account to the person's Inkcast. The MCP and REST replies state the configured lifetime. The
default is exactly the 900 s that were hard-coded before (``TICKET_TTL``, ``LINK_TTL`` and the vault's default). The
sign-in and return windows (``RETURN_TTL``, the consent page) are protocol windows and stay constant."""

import asyncio
import contextlib
import dataclasses
import pathlib
import time
import types
import unittest
from unittest import mock

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from inkcast.launcher import build, resolve
from inkcast.libs.account import RETURN_TTL, deletion_link
from inkcast.libs.config import ONE_TIME_LINK_MINUTES, Config, ConfigError, load_config, one_time_link_problems
from inkcast.libs.db import Store
from inkcast.libs.http.app import compose
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import load_platforms
from inkcast.services.publishing.usecases import InkcastService
from test_e2e import free_port, serve

ROOT = pathlib.Path(__file__).resolve().parents[1]
NAME = "INKCAST_ONE_TIME_LINK_MINUTES"
RULE = f"{NAME} must be a whole number of minutes from 5 to 60"
SECRET_KEY = "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE"
BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": SECRET_KEY,
        "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
# A Keycloak sign-in with the user sync configured: what the deletion link needs (libs/account). Nothing here reaches
# the issuer: discovery is lazy, and neither link contacts Keycloak.
KEYCLOAK = {"login": "oidc", "oidc_issuer": "https://sso.example.com/realms/inkcast", "oidc_client_id": "inkcast",
            "oidc_client_secret": "kc-secret", "oidc_required_role": "inkcast-user",
            "keycloak_sync_client_id": "inkcast-sync", "keycloak_sync_secret": "sync-secret"}
SERVICES = "IdentityService,PublishingApi,ConnectorService"   # both links, both REST endpoints, the connect page
TODAY = 900                                                   # the seconds hard-coded before this story


def config(**env) -> Config:
    return load_config({**BASE, **env})


def by_hand(**fields) -> Config:
    return Config(**{"public_url": "http://localhost:8787", "secret_key": "k" * 40, "database_path": ":memory:",
                     **KEYCLOAK, **fields})


def in_process(names: str, cfg: Config):
    """(ASGI app, context) for these services, as the launcher builds them."""
    specs = resolve(names)
    ctx = build(cfg, specs)
    return compose(specs, ctx), ctx


def oauth_token(store: Store) -> tuple[str, str]:
    """(an OAuth access token, its user id): what both links need (a personal access token can't ask for either)."""
    user = store.upsert_user("oidc:https://sso.example.com/realms/inkcast#owner", "owner", None, None)["id"]
    access = "ic_at_" + "a" * 43
    store.save_token(access, "access", "c1", user, ["inkcast"], None, "family-1", 3600)
    return access, user


@contextlib.contextmanager
def clock(at: float):
    """The database's clock (the vault and the identity repository read ``time.time()``) stopped at ``at``."""
    stopped = types.SimpleNamespace(time=lambda: at)
    with mock.patch("inkcast.libs.db.vault.time", stopped), mock.patch("inkcast.libs.db.identity.time", stopped):
        yield


def ticket_of(url: str, prefix: str) -> str:
    return url.rpartition(prefix)[2]


# ---- the setting at load ------------------------------------------------------------------------------------


class TheSetting(unittest.TestCase):
    def test_from_5_to_60_and_the_default_is_15(self):
        self.assertEqual(config().one_time_link_minutes, 15)
        self.assertEqual(config(INKCAST_ONE_TIME_LINK_MINUTES="").one_time_link_minutes, 15)
        for raw, value in (("5", 5), (" 20 ", 20), ("45", 45), ("60", 60)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_ONE_TIME_LINK_MINUTES=raw)
                self.assertEqual(cfg.one_time_link_minutes, value)
                self.assertEqual(cfg.one_time_link_seconds, value * 60)
                cfg.validate()

    def test_the_default_is_the_900_seconds_that_were_hard_coded(self):
        """TICKET_TTL, LINK_TTL and the vault's default were all 900: a server that sets nothing changes nothing."""
        self.assertEqual(config().one_time_link_seconds, TODAY)
        self.assertEqual(by_hand().one_time_link_seconds, TODAY)
        self.assertEqual(Config.one_time_link_minutes * 60, TODAY)

    def test_outside_the_range_or_not_a_whole_number_is_a_configuration_error_naming_the_variable(self):
        for raw in ("4", "61", "0", "-15", "120", "900", "15.5", "fifteen", "1e1", "0xf", "15,30", "inf", "nan", "true",
                    "off", "15m"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, RULE) as caught:
                    config(INKCAST_ONE_TIME_LINK_MINUTES=raw)
                message = str(caught.exception)
                self.assertIn(repr(raw), message, "the error quotes the value it refused")
                for other in ("INKCAST_REFRESH_TOKEN_DAYS", "INKCAST_PAT_", "INKCAST_OUTBOX", "RETURN"):
                    self.assertNotIn(other, message, "only the variable at fault is named")

    def test_sixty_is_a_hard_maximum_and_five_the_floor(self):
        """A connect link is a bearer capability that passes through the AI transcript: an hour is the most a leaked
        transcript may be good for, whatever an operator would prefer."""
        self.assertEqual(ONE_TIME_LINK_MINUTES, (5, 60))
        config(INKCAST_ONE_TIME_LINK_MINUTES="60").validate()
        config(INKCAST_ONE_TIME_LINK_MINUTES="5").validate()
        for raw in ("61", "90", "1440", "4", "1"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, RULE):
                    config(INKCAST_ONE_TIME_LINK_MINUTES=raw)

    def test_for_a_config_built_by_hand_validate_applies_the_same_rule(self):
        for minutes in (5, 15, 45, 60):
            with self.subTest(minutes=minutes):
                by_hand(one_time_link_minutes=minutes).validate()
        for minutes in (4, 61, 0, -1, 15.0, "15", True, None):
            with self.subTest(minutes=minutes):
                with self.assertRaisesRegex(ConfigError, RULE) as caught:
                    by_hand(one_time_link_minutes=minutes).validate()
                self.assertIn(repr(minutes), str(caught.exception))
        self.assertEqual(one_time_link_problems(15), [])
        [problem] = one_time_link_problems(61)
        self.assertTrue(problem.startswith(RULE), problem)

    def test_every_problem_is_listed_with_the_others(self):
        with self.assertRaises(ConfigError) as caught:
            by_hand(one_time_link_minutes=61, refresh_token_days=0).validate()
        message = str(caught.exception)
        self.assertIn(RULE, message)
        self.assertIn("INKCAST_REFRESH_TOKEN_DAYS must", message)

    def test_the_code_and_the_docs_say_why_sixty_is_the_ceiling(self):
        """The README row, .env.example, the architecture page and the setting's own comment all name the variable
        and say what the cap protects against: a connect link is a bearer capability in the AI transcript."""
        for path, must in (("README.md", f"| `{NAME}` | `15` |"), (".env.example", f"# {NAME}=15"),
                           ("docs/architecture.md", f"`{NAME}`"), ("src/inkcast/libs/config.py", NAME)):
            with self.subTest(path=path):
                text = (ROOT / path).read_text()
                self.assertIn(must, text)
                self.assertIn("transcript", text)
                self.assertRegex(text, r"(?i)bearer capability")
        row = next(ln for ln in (ROOT / "README.md").read_text().splitlines() if ln.startswith(f"| `{NAME}`"))
        self.assertIn("`5` to `60`", row)
        self.assertIn("transcript", row)


# ---- a link expires at the configured time, and not before ------------------------------------------------------


class ConnectLinkExpiry(unittest.TestCase):
    """``InkcastService.connect_link`` (the MCP tool and ``POST /api/v1/platforms/{id}/connect-link`` call it) makes a
    ticket that lasts exactly the configured lifetime: found a second before it, gone a second after."""

    def service(self, store: Store, seconds: int) -> InkcastService:
        platforms = load_platforms(httpx2.AsyncClient())
        return InkcastService(store, platforms, Publisher(store, platforms), "http://localhost:8787", seconds)

    def test_a_connect_link_lasts_the_configured_minutes(self):
        for minutes in (5, 15, 60):
            with self.subTest(minutes=minutes):
                store = Store(":memory:", "k" * 40)
                self.addCleanup(store.close)
                user = store.upsert_user("local:owner", "owner", None, None)["id"]
                service = self.service(store, minutes * 60)
                made = 1_800_000_000.0
                with clock(made):
                    link = service.connect_link(user, "devto")
                self.assertEqual(link.expires_in, minutes * 60, "the reply states the configured lifetime")
                ticket = ticket_of(link.url, "/connect/")
                row = store._one("SELECT expires_at FROM connect_tickets WHERE user_id = ?", user)
                self.assertEqual(row["expires_at"], made + minutes * 60)
                with clock(made + minutes * 60 - 1):
                    self.assertIsNotNone(store.get_ticket(ticket), "valid until the configured time")
                with clock(made + minutes * 60 + 1):
                    self.assertIsNone(store.get_ticket(ticket), "gone after it")

    def test_the_old_fifteen_minutes_is_neither_a_floor_nor_a_ceiling(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        user = store.upsert_user("local:owner", "owner", None, None)["id"]
        made = 1_800_000_000.0
        with clock(made):
            long = ticket_of(self.service(store, 3600).connect_link(user, "devto").url, "/connect/")
            short = ticket_of(self.service(store, 300).connect_link(user, "medium").url, "/connect/")
        with clock(made + TODAY + 1):
            self.assertIsNotNone(store.get_ticket(long), "an hour's link is still valid after the old 15 minutes")
            self.assertIsNone(store.get_ticket(short), "a 5-minute link is gone by then")
        with clock(made + 3601):
            self.assertIsNone(store.get_ticket(long))


class DeletionLinkExpiry(unittest.TestCase):
    """``deletion_link`` (the MCP tool and ``POST /api/v1/account/deletion-link`` call it) reads the same setting; the
    30 minutes from Continue to the visit back from Keycloak (``RETURN_TTL``) is a protocol window and stays."""

    def test_a_deletion_link_lasts_the_configured_minutes(self):
        for minutes in (5, 15, 60):
            with self.subTest(minutes=minutes):
                store = Store(":memory:", "k" * 40)
                self.addCleanup(store.close)
                cfg = by_hand(one_time_link_minutes=minutes)
                user = store.upsert_user("oidc:https://sso.example.com/realms/inkcast#u", "u", None, None)["id"]
                made = 1_800_000_000.0
                with clock(made):
                    early, late = deletion_link(cfg, store, user), deletion_link(cfg, store, user)
                self.assertEqual((early.expires_in, late.expires_in), (minutes * 60, minutes * 60))
                self.assertTrue(early.url.startswith("http://localhost:8787/account/delete/"), early.url)
                for row in store._all("SELECT expires_at FROM deletion_requests WHERE user_id = ?", user):
                    self.assertEqual(row["expires_at"], made + minutes * 60)
                with clock(made + minutes * 60 - 1):
                    self.assertIsNotNone(store.open_deletion_link(ticket_of(early.url, "/account/delete/")), "valid until then")
                with clock(made + minutes * 60 + 1):
                    self.assertIsNone(store.open_deletion_link(ticket_of(late.url, "/account/delete/")), "gone after it")

    def test_the_return_window_is_not_the_setting(self):
        self.assertEqual(RETURN_TTL, 1800)
        for minutes in (5, 60):
            self.assertNotEqual(by_hand(one_time_link_minutes=minutes).one_time_link_seconds, RETURN_TTL)


class ThroughTheStack(unittest.TestCase):
    """The whole journey in one process under a 60-minute setting: the REST reply, the row, and the pages, which
    still open after the old 15 minutes and refuse the link after the configured hour. The clock is stopped at the
    real time the link is made (the OAuth token that asks for it has an hour of its own), then moved."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = by_hand(one_time_link_minutes=60)
        app, cls.ctx = in_process(SERVICES, cls.cfg)
        cls.access, cls.user = oauth_token(cls.ctx.store)
        cls.client = TestClient(app, base_url=cls.cfg.public_url).__enter__()   # one lifespan for the class

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.ctx.store.close()

    def post(self, path: str) -> httpx2.Response:
        return self.client.post(path, headers={"Authorization": f"Bearer {self.access}"})

    def test_the_connect_page_opens_until_the_configured_hour(self):
        made = time.time()
        with clock(made):
            r = self.post("/api/v1/platforms/devto/connect-link")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["expires_in"], 3600, "the REST reply states the configured lifetime")
        ticket = ticket_of(r.json()["url"], "/connect/")
        row = self.ctx.store._one("SELECT expires_at FROM connect_tickets WHERE user_id = ?", self.user)
        self.assertEqual(row["expires_at"], made + 3600)
        with clock(made + TODAY + 1):
            page = self.client.get(f"/connect/{ticket}")
        self.assertEqual(page.status_code, 200, "still open after the old 15 minutes")
        self.assertIn("api_key", page.text)
        with clock(made + 3600 + 1):
            expired = self.client.get(f"/connect/{ticket}")
        self.assertEqual(expired.status_code, 400)
        self.assertIn("Link expired", expired.text)

    def test_the_deletion_link_lasts_the_configured_hour(self):
        made = time.time()
        with clock(made):
            r = self.post("/api/v1/account/deletion-link")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["expires_in"], 3600)
        ticket = ticket_of(r.json()["url"], "/account/delete/")
        with clock(made + 3600 + 1):
            expired = self.client.get(f"/account/delete/{ticket}")
        self.assertEqual(expired.status_code, 400)
        self.assertIn("Link expired", expired.text)
        with clock(made + TODAY + 1):
            self.assertIsNotNone(self.ctx.store.open_deletion_link(ticket),
                                 "the expired visit consumed nothing; after the old 15 minutes the link still opens")


# ---- the MCP and REST replies state the configured lifetime ---------------------------------------------------


class RepliesStateTheLifetime(unittest.TestCase):
    """Over real HTTP, signed in with an OAuth token: ``inkcast_connect`` and ``inkcast_delete_account`` say "valid N
    minutes, one use" for the configured N, and both REST endpoints answer ``expires_in`` of N × 60."""

    minutes = 45

    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.cfg = by_hand(public_url=f"http://127.0.0.1:{port}", port=port, secret_key="k" * 48,
                          **({"one_time_link_minutes": cls.minutes} if cls.minutes else {}))
        cls.cfg.validate()
        specs = resolve(SERVICES)
        cls.ctx = build(cls.cfg, specs)
        cls.server = serve(compose(specs, cls.ctx), port)
        cls.access, cls.user = oauth_token(cls.ctx.store)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.ctx.store.close()

    @property
    def expected(self) -> int:
        return self.minutes or 15

    def tool(self, name: str, args: dict) -> tuple[bool, str]:
        async def run():
            async with httpx2.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.access}"}) as http:
                async with streamable_http_client(self.cfg.mcp_url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        r = await session.call_tool(name, args)
            return r.is_error, "\n".join(c.text for c in r.content if hasattr(c, "text"))
        return asyncio.run(run())

    def rest(self, path: str) -> dict:
        r = httpx2.post(f"{self.cfg.public_url}{path}", headers={"Authorization": f"Bearer {self.access}"}, timeout=30)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_inkcast_connect_states_the_configured_minutes(self):
        error, text = self.tool("inkcast_connect", {"platform_id": "devto"})
        self.assertFalse(error, text)
        self.assertIn(f"valid {self.expected} minutes, one use", text)
        self.assertIn(f"{self.cfg.public_url}/connect/", text)
        self.assertNotIn(f"valid {self.expected + 1} minutes", text)

    def test_inkcast_delete_account_states_the_configured_minutes(self):
        error, text = self.tool("inkcast_delete_account", {})
        self.assertFalse(error, text)
        self.assertIn(f"valid {self.expected} minutes, one use", text)
        self.assertIn(f"{self.cfg.public_url}/account/delete/", text)
        self.assertIn("Nothing has been deleted yet", text)

    def test_the_rest_replies_state_the_configured_seconds(self):
        connect = self.rest("/api/v1/platforms/devto/connect-link")
        self.assertEqual(connect["expires_in"], self.expected * 60)
        self.assertTrue(connect["url"].startswith(f"{self.cfg.public_url}/connect/"), connect)
        deletion = self.rest("/api/v1/account/deletion-link")
        self.assertEqual(deletion["expires_in"], self.expected * 60)
        self.assertTrue(deletion["url"].startswith(f"{self.cfg.public_url}/account/delete/"), deletion)


class RepliesWithTheDefault(RepliesStateTheLifetime):
    """A server that sets nothing says "valid 15 minutes" and answers 900, exactly as before this story."""

    minutes = 0

    def test_the_default_is_what_was_hard_coded(self):
        self.assertEqual(self.cfg.one_time_link_seconds, TODAY)
        self.assertEqual(self.rest("/api/v1/account/deletion-link")["expires_in"], TODAY)


class ConfigIsFrozen(unittest.TestCase):
    def test_the_setting_is_a_value_on_the_frozen_config(self):
        cfg = dataclasses.replace(by_hand(), one_time_link_minutes=30)
        self.assertEqual(cfg.one_time_link_seconds, 1800)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.one_time_link_minutes = 10


if __name__ == "__main__":
    unittest.main()
