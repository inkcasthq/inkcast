# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""F1.10.S2 (issue #90): token lifetimes are settings.

Personal access tokens: ``INKCAST_PAT_DEFAULT_DAYS`` (90) when the request doesn't say, at most ``INKCAST_PAT_MAX_DAYS``
(366, the absolute ceiling, which stays in the API document), and never only while ``INKCAST_PAT_ALLOW_NO_EXPIRY`` is
true. Every entry point (REST, the /tokens/new page, the MCP tool) enforces the configured limit where the token is
made, with a 422 (a tool error) naming it. MCP sign-in: a newly issued refresh token lasts ``INKCAST_REFRESH_TOKEN_DAYS``
(30, 1-90); the access token's hour stays fixed. The defaults are exactly what was hard-coded before."""

import asyncio
import dataclasses
import pathlib
import re
import time
import unittest
from urllib.parse import parse_qs, urlparse

import httpx2
import yaml
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from inkcast.launcher import build, gate, resolve
from inkcast.libs.config import PAT_DAYS_CEILING, Config, ConfigError, PatPolicy, load_config
from inkcast.libs.contracts.models import TokenCreate
from inkcast.libs.contracts.openapi import openapi_document
from inkcast.libs.db import Store
from inkcast.libs.http.app import compose
from inkcast.libs.secrets import Redactor, run_checks
from inkcast.services.identity import TOKEN_REQUEST
from inkcast.services.identity.provider import InkcastOAuthProvider
from test_e2e import free_port, serve

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = {"INKCAST_PUBLIC_URL": "https://inkcast.example.com", "INKCAST_SECRET_KEY": "Q7r!x9Lm2vP0sZ4bN8cH1dK5fG3jT6wY-aE",
        "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": "p" * 12}
DEFAULT, MAX, NEVER, REFRESH = ("INKCAST_PAT_DEFAULT_DAYS", "INKCAST_PAT_MAX_DAYS", "INKCAST_PAT_ALLOW_NO_EXPIRY",
                                "INKCAST_REFRESH_TOKEN_DAYS")
PASSWORD = "p" * 12
DAY = 86400
# Before this story: TokenCreate's Field(90, ge=1, le=366), "never" through REST's null, refresh_token_seconds = 30 days.
TODAY = PatPolicy(default_days=90, max_days=366, allow_no_expiry=True)
# A server that chose its own policy: a shorter default, a lower maximum, and no token that never expires.
STRICT = PatPolicy(default_days=45, max_days=120, allow_no_expiry=False)


def config(**env) -> Config:
    return load_config({**BASE, **env})


def by_hand(**fields) -> Config:
    return Config(**{"public_url": "http://localhost:8787", "secret_key": "k" * 40, "database_path": ":memory:",
                     "login": "password", "password": PASSWORD, **fields})


def in_process(names: str, cfg: Config):
    """(ASGI app, context) for these services, as the launcher builds them."""
    specs = resolve(names)
    ctx = build(cfg, specs)
    return compose(specs, ctx), ctx


def form_fields(html: str) -> dict:
    return dict(re.findall(r'name=(\w+) value="([^"]*)"', html))


def oauth_token(store: Store) -> tuple[str, str]:
    """(an OAuth access token, its user id): what token management needs (a PAT can't mint PATs)."""
    user = store.upsert_user("local:owner", "owner", None, None)["id"]
    access = "ic_at_" + "a" * 43
    store.save_token(access, "access", "c1", user, ["inkcast"], None, "family-1", 3600)
    return access, user


def assert_names_the_maximum(test: unittest.TestCase, message: str, policy: PatPolicy) -> None:
    test.assertIn(f"{policy.max_days} days", message)
    test.assertIn(MAX, message)


def assert_names_no_expiry(test: unittest.TestCase, message: str) -> None:
    test.assertIn(NEVER, message)
    test.assertIn("must expire", message)


# ---- the settings at load --------------------------------------------------------------------------------


class PatMaxDays(unittest.TestCase):
    def test_from_1_to_366_and_the_default_is_366(self):
        self.assertEqual(config().pat.max_days, 366)
        self.assertEqual(config(INKCAST_PAT_MAX_DAYS="").pat.max_days, 366)
        for raw, value in (("366", 366), (" 180 ", 180), ("90", 90), ("1", 1)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_PAT_MAX_DAYS=raw, INKCAST_PAT_DEFAULT_DAYS="1")
                self.assertEqual(cfg.pat.max_days, value)
                cfg.validate()

    def test_outside_the_range_or_not_a_whole_number_is_a_configuration_error_naming_the_variable(self):
        for raw in ("0", "367", "-1", "1000", "90.5", "ninety", "1e2", "0x5a", "90,180", "inf", "true", "never"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{MAX} must be a whole number of days from 1 to 366") as caught:
                    config(INKCAST_PAT_MAX_DAYS=raw, INKCAST_PAT_DEFAULT_DAYS="1")
                self.assertIn(repr(raw), str(caught.exception), "the error quotes the value it refused")
                self.assertNotIn(DEFAULT, str(caught.exception), "only the variable at fault is named")
                self.assertNotIn(REFRESH, str(caught.exception))


class PatDefaultDays(unittest.TestCase):
    def test_from_1_to_the_maximum_and_the_default_is_90(self):
        self.assertEqual(config().pat.default_days, 90)
        self.assertEqual(config(INKCAST_PAT_DEFAULT_DAYS="").pat.default_days, 90)
        for raw, value in (("1", 1), (" 30 ", 30), ("366", 366)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_PAT_DEFAULT_DAYS=raw)
                self.assertEqual(cfg.pat.default_days, value)
                cfg.validate()

    def test_outside_the_range_or_not_a_whole_number_is_a_configuration_error_naming_the_variable(self):
        for raw in ("0", "367", "-90", "45.5", "ninety", "1e1", "0x5a", "30,60", "nan", "false", "never"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{DEFAULT} must be a whole number of days from 1 to") as caught:
                    config(INKCAST_PAT_DEFAULT_DAYS=raw)
                self.assertIn(repr(raw), str(caught.exception), "the error quotes the value it refused")
                self.assertNotIn(MAX + " must", str(caught.exception), "only the variable at fault is named")

    def test_the_default_may_not_exceed_the_maximum(self):
        config(INKCAST_PAT_DEFAULT_DAYS="30", INKCAST_PAT_MAX_DAYS="30").validate()        # equal is fine
        for default, maximum in (("31", "30"), ("90", "60"), ("366", "365"), ("2", "1")):
            with self.subTest(default=default, maximum=maximum):
                with self.assertRaises(ConfigError) as caught:
                    config(INKCAST_PAT_DEFAULT_DAYS=default, INKCAST_PAT_MAX_DAYS=maximum)
                message = str(caught.exception)
                self.assertIn(f"{DEFAULT} must be a whole number of days from 1 to {MAX} ({maximum})", message)
                self.assertIn(repr(default), message)
                self.assertNotIn(f"{MAX} must", message, "the maximum itself is fine")
        # The unchanged default (90) is over a lower maximum: that is a refusal too, naming what to set.
        with self.assertRaisesRegex(ConfigError, f"{DEFAULT} must be a whole number of days from 1 to {MAX} \\(60\\)"):
            config(INKCAST_PAT_MAX_DAYS="60")

    def test_both_wrong_names_both(self):
        with self.assertRaises(ConfigError) as caught:
            config(INKCAST_PAT_DEFAULT_DAYS="0", INKCAST_PAT_MAX_DAYS="367")
        self.assertIn(f"{DEFAULT} must", str(caught.exception))
        self.assertIn(f"{MAX} must", str(caught.exception))


class PatAllowNoExpiry(unittest.TestCase):
    def test_true_or_false_and_the_default_is_true(self):
        self.assertTrue(config().pat.allow_no_expiry)
        self.assertTrue(config(INKCAST_PAT_ALLOW_NO_EXPIRY="").pat.allow_no_expiry)
        for raw, value in (("true", True), ("True", True), ("yes", True), ("on", True), ("1", True),
                           ("false", False), ("FALSE", False), ("no", False), ("off", False), ("0", False), (" false ", False)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_PAT_ALLOW_NO_EXPIRY=raw)
                self.assertIs(cfg.pat.allow_no_expiry, value)
                cfg.validate()

    def test_anything_else_is_a_configuration_error_naming_the_variable_not_a_silent_false(self):
        for raw in ("ture", "flase", "never", "2", "none", "null", "y", "n", "t", "f"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{NEVER} must be true or false") as caught:
                    config(INKCAST_PAT_ALLOW_NO_EXPIRY=raw)
                self.assertIn(repr(raw), str(caught.exception))
                self.assertNotIn(DEFAULT, str(caught.exception))
                self.assertNotIn(MAX, str(caught.exception))


class RefreshTokenDays(unittest.TestCase):
    def test_from_1_to_90_and_the_default_is_30(self):
        self.assertEqual(config().refresh_token_days, 30)
        self.assertEqual(config(INKCAST_REFRESH_TOKEN_DAYS="").refresh_token_days, 30)
        for raw, value in (("1", 1), (" 7 ", 7), ("90", 90)):
            with self.subTest(raw=raw):
                cfg = config(INKCAST_REFRESH_TOKEN_DAYS=raw)
                self.assertEqual(cfg.refresh_token_days, value)
                cfg.validate()

    def test_outside_the_range_or_not_a_whole_number_is_a_configuration_error_naming_the_variable(self):
        for raw in ("0", "91", "-30", "365", "7.5", "thirty", "1e1", "0x1e", "7,14", "inf", "true", "off"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ConfigError, f"{REFRESH} must be a whole number of days from 1 to 90") as caught:
                    config(INKCAST_REFRESH_TOKEN_DAYS=raw)
                self.assertIn(repr(raw), str(caught.exception), "the error quotes the value it refused")
                self.assertNotIn("PAT", str(caught.exception), "only the variable at fault is named")


class ConfigBuiltByHand(unittest.TestCase):
    def test_validate_applies_the_same_rules(self):
        for policy in (TODAY, STRICT, PatPolicy(1, 1, False), PatPolicy(366, 366, True), PatPolicy(30, 30, True)):
            with self.subTest(policy=policy):
                by_hand(pat=policy).validate()
        for days in (1, 30, 90):
            by_hand(refresh_token_days=days).validate()
        for policy, name in (
            (PatPolicy(0, 366, True), DEFAULT), (PatPolicy(91, 90, True), DEFAULT), (PatPolicy(90.0, 366, True), DEFAULT),
            (PatPolicy("90", 366, True), DEFAULT), (PatPolicy(True, 366, True), DEFAULT),
            (PatPolicy(90, 367, True), MAX), (PatPolicy(1, 0, True), MAX), (PatPolicy(90, 366.0, True), MAX),
            (PatPolicy(90, "366", True), MAX),
            (PatPolicy(90, 366, "true"), NEVER), (PatPolicy(90, 366, 1), NEVER), (PatPolicy(90, 366, None), NEVER),
        ):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ConfigError, f"{name} must"):
                    by_hand(pat=policy).validate()
        for days in (0, 91, -1, 30.0, "30", True, None):
            with self.subTest(days=days):
                with self.assertRaisesRegex(ConfigError, f"{REFRESH} must"):
                    by_hand(refresh_token_days=days).validate()


# ---- the defaults are today's behaviour --------------------------------------------------------------------


class Defaults(unittest.TestCase):
    def test_the_settings_default_to_what_was_hard_coded(self):
        self.assertEqual(PatPolicy(), TODAY)
        self.assertEqual(config().pat, TODAY)
        self.assertEqual(by_hand().pat, TODAY)
        self.assertEqual((config().refresh_token_days, by_hand().refresh_token_days), (30, 30))
        self.assertEqual(PAT_DAYS_CEILING, 366)

    def test_the_access_token_hour_is_not_a_setting(self):
        cfg = config(INKCAST_ACCESS_TOKEN_SECONDS="60", INKCAST_ACCESS_TOKEN_DAYS="2", INKCAST_ACCESS_TOKEN_MINUTES="1")
        self.assertEqual(cfg.access_token_seconds, 3600)
        source = (ROOT / "src" / "inkcast" / "libs" / "config.py").read_text()
        self.assertNotIn("INKCAST_ACCESS_TOKEN", source)
        self.assertRegex(source, r"access_token_seconds: int = 3600", "the hour stays fixed")
        self.assertRegex(source, r"(?i)deliberately not", "the field says why it is not configurable")

    def test_the_api_document_keeps_366_as_the_absolute_ceiling(self):
        field = TokenCreate.model_fields["expires_in_days"]
        self.assertEqual(field.default, 90)
        self.assertEqual([m.le for m in field.metadata if hasattr(m, "le")], [PAT_DAYS_CEILING])
        schema = openapi_document("https://inkcast.example.com")["components"]["schemas"]["TokenCreate"]["properties"]["expires_in_days"]
        [integer] = [s for s in schema["anyOf"] if s.get("type") == "integer"]
        self.assertEqual((integer["minimum"], integer["maximum"]), (1, 366))
        self.assertIn({"type": "null"}, schema["anyOf"])


# ---- the three entry points ----------------------------------------------------------------------------------


class RestEntryPoint(unittest.TestCase):
    """POST /api/v1/tokens with an OAuth token."""

    def client(self, policy: PatPolicy):
        cfg = by_hand(pat=policy)
        app, ctx = in_process("IdentityService", cfg)
        self.addCleanup(ctx.store.close)
        access, user = oauth_token(ctx.store)
        c = TestClient(app, base_url=cfg.public_url, headers={"Authorization": f"Bearer {access}"})
        return c, ctx.store, user

    def test_omitted_means_the_configured_default(self):
        for policy in (STRICT, TODAY, PatPolicy(7, 7, False)):
            with self.subTest(policy=policy):
                c, store, user = self.client(policy)
                before = time.time()
                r = c.post("/api/v1/tokens", json={"name": "ci", "scopes": ["stories:sync"]})
                self.assertEqual(r.status_code, 200, r.text)
                [row] = store.list_api_tokens(user)
                self.assertAlmostEqual(row["expires_at"] - before, policy.default_days * DAY, delta=5)
                self.assertTrue(r.json()["expires_at"])

    def test_over_the_configured_maximum_is_422_naming_the_limit(self):
        for policy in (STRICT, PatPolicy(1, 1, True), PatPolicy(30, 365, True)):
            with self.subTest(policy=policy):
                c, store, user = self.client(policy)
                for days in (policy.max_days + 1, 366):
                    r = c.post("/api/v1/tokens", json={"name": "ci", "scopes": ["stories:sync"], "expires_in_days": days})
                    self.assertEqual(r.status_code, 422, r.text)
                    self.assertEqual(r.json()["error"], "invalid_expiry")
                    assert_names_the_maximum(self, r.json()["message"], policy)
                self.assertEqual(store.list_api_tokens(user), [], "a refused request made a token")
                at_most = c.post("/api/v1/tokens", json={"name": "ci", "scopes": ["stories:sync"], "expires_in_days": policy.max_days})
                self.assertEqual(at_most.status_code, 200, at_most.text)

    def test_the_absolute_ceiling_is_still_the_api_contract(self):
        c, _, _ = self.client(TODAY)
        r = c.post("/api/v1/tokens", json={"name": "ci", "scopes": ["stories:sync"], "expires_in_days": 367})
        self.assertEqual((r.status_code, r.json()["error"]), (422, "invalid_request"), r.text)
        self.assertEqual(c.post("/api/v1/tokens", json={"name": "ci", "scopes": ["stories:sync"], "expires_in_days": 366}).status_code, 200)

    def test_never_is_refused_when_not_allowed_and_works_when_it_is(self):
        c, store, user = self.client(STRICT)
        r = c.post("/api/v1/tokens", json={"name": "forever", "scopes": ["stories:sync"], "expires_in_days": None})
        self.assertEqual((r.status_code, r.json()["error"]), (422, "invalid_expiry"), r.text)
        assert_names_no_expiry(self, r.json()["message"])
        self.assertEqual(store.list_api_tokens(user), [])
        c, store, user = self.client(TODAY)
        r = c.post("/api/v1/tokens", json={"name": "forever", "scopes": ["stories:sync"], "expires_in_days": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["expires_at"])
        self.assertIsNone(store.list_api_tokens(user)[0]["expires_at"])


class TokenPageEntryPoint(unittest.TestCase):
    """/tokens/new: the link the MCP tool hands out, which anyone can also type. Password sign-in, then the confirm
    page, then the token shown once."""

    def client(self, policy: PatPolicy):
        cfg = by_hand(pat=policy)
        app, ctx = in_process("IdentityService", cfg)
        self.addCleanup(ctx.store.close)
        return TestClient(app, base_url=cfg.public_url), ctx.store

    def sign_in(self, c: TestClient, pending: str):
        """The password form's POST: the redirect to the consent (confirm) page."""
        r = c.post("/auth/callback", data={"pending": pending, "password": PASSWORD}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertIn("/auth/consent", r.headers["location"])
        return r.headers["location"]

    def approve(self, c: TestClient, page: str) -> httpx2.Response:
        return c.post("/auth/consent", data={**form_fields(c.get(page).text), "decision": "approve"})

    def test_no_days_means_the_configured_default(self):
        for policy in (STRICT, TODAY):
            with self.subTest(policy=policy):
                c, store = self.client(policy)
                form = c.get("/tokens/new", params={"name": "laptop", "scopes": "stories:sync"})
                self.assertEqual(form.status_code, 200, form.text)
                confirm = self.sign_in(c, form_fields(form.text)["pending"])
                self.assertIn(f"Expires: in {policy.default_days} days", c.get(confirm).text)
                before = time.time()
                done = self.approve(c, confirm)
                self.assertRegex(done.text, r"ink_pat_[A-Za-z0-9_\-]+")
                [row] = store.list_api_tokens(store.find_user_by_identity("local:owner")["id"])
                self.assertAlmostEqual(row["expires_at"] - before, policy.default_days * DAY, delta=5)

    def test_over_the_configured_maximum_is_422_naming_the_limit(self):
        for policy in (STRICT, PatPolicy(1, 1, True)):
            with self.subTest(policy=policy):
                c, store = self.client(policy)
                for days in (policy.max_days + 1, 366):
                    r = c.get("/tokens/new", params={"name": "ci", "scopes": "stories:sync", "days": str(days)})
                    self.assertEqual(r.status_code, 422, r.text)
                    assert_names_the_maximum(self, r.text, policy)
                    self.assertNotIn("name=pending", r.text, "a refused request started a sign-in")
                self.assertEqual(store._one("SELECT count(*) AS n FROM pending_auth")["n"], 0)
                ok = c.get("/tokens/new", params={"name": "ci", "scopes": "stories:sync", "days": str(policy.max_days)})
                self.assertEqual(ok.status_code, 200, ok.text)

    def test_never_is_refused_when_not_allowed_and_works_when_it_is(self):
        c, store = self.client(STRICT)
        r = c.get("/tokens/new", params={"name": "forever", "scopes": "stories:sync", "days": "never"})
        self.assertEqual(r.status_code, 422, r.text)
        assert_names_no_expiry(self, r.text)
        c, store = self.client(TODAY)
        form = c.get("/tokens/new", params={"name": "forever", "scopes": "stories:sync", "days": "never"})
        self.assertEqual(form.status_code, 200, form.text)
        confirm = self.sign_in(c, form_fields(form.text)["pending"])
        self.assertIn("Expires: never", c.get(confirm).text)
        self.assertRegex(self.approve(c, confirm).text, r"ink_pat_[A-Za-z0-9_\-]+")
        self.assertIsNone(store.list_api_tokens(store.find_user_by_identity("local:owner")["id"])[0]["expires_at"])

    def test_the_policy_is_enforced_where_the_token_is_made_not_only_at_the_link(self):
        """A pending request made under an older, looser policy (a restart with tighter settings in between) is held to
        the current one at the confirm page and at the approval: the stale link mints nothing."""
        c, store = self.client(STRICT)
        for params in (TokenCreate(name="old", scopes=["stories:sync"], expires_in_days=None),
                       TokenCreate(name="old", scopes=["stories:sync"], expires_in_days=STRICT.max_days + 1)):
            with self.subTest(params=params):
                pending = store.create_pending(TOKEN_REQUEST, params.model_dump_json())
                confirm = self.sign_in(c, pending)
                page = c.get(confirm)
                self.assertEqual(page.status_code, 422, page.text)
                self.assertNotIn("value=approve", page.text, "the stale request got a confirm button")
                self.assertIn("Token not created", page.text)
                again = c.post("/auth/consent", data={"pending": pending, "csrf": store.get_pending(pending)["csrf"]
                                                     if store.get_pending(pending) else "", "decision": "approve"})
                self.assertNotRegex(again.text, r"ink_pat_")
        self.assertEqual(store.list_api_tokens(store.find_user_by_identity("local:owner")["id"]), [])


class McpEntryPoint(unittest.TestCase):
    """inkcast_create_token over real HTTP, signed in with an OAuth token: it refuses what the page would refuse,
    so the link it hands out is never a dead end, and its schema states this server's default."""

    policy = STRICT

    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.config = Config(public_url=f"http://127.0.0.1:{port}", port=port, secret_key="k" * 48, database_path=":memory:",
                            login="password", password=PASSWORD, pat=cls.policy)
        specs = resolve("PublishingApi")
        cls.ctx = build(cls.config, specs)
        cls.server = serve(compose(specs, cls.ctx), port)
        cls.access, cls.user = oauth_token(cls.ctx.store)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def tool(self, args: dict) -> tuple[bool, str]:
        async def run():
            async with httpx2.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.access}"}) as http:
                async with streamable_http_client(self.config.mcp_url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        r = await session.call_tool("inkcast_create_token", args)
            return r.is_error, "\n".join(c.text for c in r.content if hasattr(c, "text"))
        return asyncio.run(run())

    def link_days(self, text: str) -> str:
        link = next(t for t in text.split() if t.startswith(f"{self.config.public_url}/tokens/new?"))
        return parse_qs(urlparse(link).query)["days"][0]

    def test_omitted_means_the_configured_default(self):
        error, text = self.tool({"name": "blog repo CI", "scopes": ["stories:sync"]})
        self.assertFalse(error, text)
        self.assertEqual(self.link_days(text), str(self.policy.default_days))
        self.assertIn(f"{self.policy.default_days} days", text)
        tools = {t.name: t for t in asyncio.run(self.ctx.extras["mcp"].list_tools())}
        tool = tools["inkcast_create_token"]
        prop = tool.input_schema["properties"]["expires_in_days"]
        self.assertEqual(prop["default"], self.policy.default_days, "the schema states this server's default")
        self.assertIn(f"lasts {self.policy.default_days} days", tool.description)
        self.assertIn(f"at most {self.policy.max_days}", tool.description, "the tool states this server's maximum")
        self.assertIn("null = never" if self.policy.allow_no_expiry else "null (never) is refused", tool.description)
        [integer] = [s for s in prop["anyOf"] if s.get("type") == "integer"]
        self.assertEqual(integer["maximum"], PAT_DAYS_CEILING, "the schema's ceiling stays absolute; the limit is enforced")
        self.assertIn({"type": "null"}, prop["anyOf"])

    def test_over_the_configured_maximum_is_a_tool_error_naming_the_limit(self):
        for days in (self.policy.max_days + 1, 366):
            with self.subTest(days=days):
                error, text = self.tool({"name": "ci", "scopes": ["stories:sync"], "expires_in_days": days})
                self.assertTrue(error, text)
                assert_names_the_maximum(self, text, self.policy)
                self.assertNotIn("/tokens/new?", text)
        error, text = self.tool({"name": "ci", "scopes": ["stories:sync"], "expires_in_days": self.policy.max_days})
        self.assertFalse(error, text)
        self.assertEqual(self.link_days(text), str(self.policy.max_days))

    def test_never_is_refused_when_not_allowed(self):
        error, text = self.tool({"name": "forever", "scopes": ["stories:sync"], "expires_in_days": None})
        self.assertTrue(error, text)
        assert_names_no_expiry(self, text)
        self.assertNotIn("/tokens/new?", text)


class McpEntryPointWithTheDefaults(McpEntryPoint):
    policy = TODAY

    def test_over_the_configured_maximum_is_a_tool_error_naming_the_limit(self):
        """The maximum is the ceiling: 366 is accepted, and 367 is the schema's refusal, as before."""
        error, text = self.tool({"name": "ci", "scopes": ["stories:sync"], "expires_in_days": 366})
        self.assertFalse(error, text)
        self.assertEqual(self.link_days(text), "366")
        error, text = self.tool({"name": "ci", "scopes": ["stories:sync"], "expires_in_days": 367})
        self.assertTrue(error, text)
        self.assertIn("366", text)
        self.assertNotIn("/tokens/new?", text)

    def test_never_is_refused_when_not_allowed(self):
        error, text = self.tool({"name": "forever", "scopes": ["stories:sync"], "expires_in_days": None})
        self.assertFalse(error, text)
        self.assertEqual(self.link_days(text), "never")
        self.assertIn("never expires", text)


# ---- MCP sign-in: refresh tokens -----------------------------------------------------------------------------


class RefreshTokens(unittest.TestCase):
    def issue(self, cfg: Config, store: Store) -> tuple[dict, dict]:
        """(the access row, the refresh row) of a sign-in under ``cfg``; the refresh token itself is in the row too."""
        user = store.upsert_user("local:owner", "owner", None, None)["id"]
        issued = InkcastOAuthProvider(cfg, store)._issue("c1", user, ["inkcast"], None, family="f-" + str(time.time()))
        refresh = {**store.get_token(issued.refresh_token, "refresh"), "value": issued.refresh_token}
        return store.get_token(issued.access_token, "access"), refresh

    def test_a_new_refresh_token_lasts_the_configured_days_and_the_access_token_its_hour(self):
        for days in (1, 7, 30, 90):
            with self.subTest(days=days):
                store = Store(":memory:", "k" * 40)
                self.addCleanup(store.close)
                before = time.time()
                access, refresh = self.issue(by_hand(refresh_token_days=days), store)
                self.assertAlmostEqual(refresh["expires_at"] - before, days * DAY, delta=5)
                self.assertAlmostEqual(access["expires_at"] - before, 3600, delta=5)

    def test_the_default_is_the_30_days_that_were_hard_coded(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        before = time.time()
        _, refresh = self.issue(by_hand(), store)
        self.assertAlmostEqual(refresh["expires_at"] - before, 30 * DAY, delta=5)

    def test_a_change_applies_to_newly_issued_refresh_tokens_only(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        before = time.time()
        _, old = self.issue(by_hand(refresh_token_days=60), store)
        _, new = self.issue(by_hand(refresh_token_days=3), store)       # the process restarted with a shorter setting
        self.assertAlmostEqual(old["expires_at"] - before, 60 * DAY, delta=5)
        self.assertAlmostEqual(new["expires_at"] - before, 3 * DAY, delta=5)
        still = store.get_token(old["value"], "refresh")                  # the earlier token keeps its 60 days
        self.assertEqual(still["expires_at"], old["expires_at"])


# ---- the posture check and the hosted environments -----------------------------------------------------------


class Posture(unittest.TestCase):
    def check(self, policy: PatPolicy, environment: str):
        cfg = by_hand(environment=environment, pat=policy)
        return {c.id: c for c in run_checks(cfg, redactor=Redactor(), environ={})}["pat-expiry"]

    def test_no_expiry_allowed_is_info_in_production_and_staging(self):
        for policy in (TODAY, PatPolicy(30, 90, True)):
            for environment in ("production", "staging"):
                with self.subTest(policy=policy, environment=environment):
                    check = self.check(policy, environment)
                    self.assertEqual(check.status, "info", check.detail)
                    self.assertIn(f"{NEVER}=true", check.detail)
                    self.assertIn(f"{DEFAULT}={policy.default_days}", check.detail)
                    self.assertIn(f"{MAX}={policy.max_days}", check.detail)
                    self.assertIn("never expires", check.detail)
            for environment in ("development", "ci"):
                with self.subTest(policy=policy, environment=environment):
                    self.assertEqual(self.check(policy, environment).status, "pass")

    def test_an_expiry_required_passes_everywhere(self):
        for policy in (STRICT, PatPolicy(90, 366, False)):
            for environment in ("production", "staging", "development", "ci"):
                with self.subTest(policy=policy, environment=environment):
                    check = self.check(policy, environment)
                    self.assertEqual(check.status, "pass", check.detail)
                    self.assertIn(f"{NEVER}=false", check.detail)
                    self.assertIn(f"{policy.max_days} days", check.detail)

    def test_info_never_stops_or_warns_the_launcher(self):
        import logging
        cfg = by_hand(environment="production", public_url="https://inkcast.example.com", log_format="json",
                      secret_key=BASE["INKCAST_SECRET_KEY"], authz_allow_local=True, pat=TODAY)
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("inkcast")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        gate(cfg, resolve("IdentityService"))                    # other checks may warn about this machine; none fail
        self.assertEqual([r for r in records if getattr(r, "check", "") == "pat-expiry"], [])
        self.assertEqual({c.id: c.status for c in run_checks(cfg, redactor=Redactor(), environ={})}["pat-expiry"], "info")

    def test_the_table_has_the_check(self):
        ids = [c.id for c in run_checks(by_hand(), redactor=Redactor(), environ={})]
        self.assertIn("pat-expiry", ids)
        self.assertNotIn("check crashed", " ".join(c.detail for c in run_checks(by_hand(), redactor=Redactor(), environ={})))


class HostedEnvironments(unittest.TestCase):
    """Production states the three PAT settings explicitly, so allowing a token that never expires is a choice on
    record (the posture `info`); staging rehearses production with the same values."""

    def literals(self, environment: str) -> dict[str, str]:
        k = yaml.safe_load((ROOT / "deploy" / "kustomize" / "environments" / environment / "kustomization.yaml").read_text())
        pairs = [lit.split("=", 1) for g in k.get("configMapGenerator", []) if g["name"] == "inkcast-config"
                 for lit in g.get("literals", [])]
        return {name: value for name, value in pairs}

    def test_production_and_staging_set_the_pat_settings_to_the_same_values_and_they_load(self):
        production, staging = self.literals("production"), self.literals("staging")
        for name in (DEFAULT, MAX, NEVER):
            self.assertIn(name, production, f"production leaves {name} to the default")
            self.assertIn(name, staging, f"staging leaves {name} to the default")
            self.assertEqual(production[name], staging[name], name)
        chosen = {name: production[name] for name in (DEFAULT, MAX, NEVER)}
        cfg = config(**chosen)
        cfg.validate()
        self.assertLessEqual(cfg.pat.default_days, cfg.pat.max_days)
        self.assertLessEqual(cfg.pat.max_days, PAT_DAYS_CEILING)


class ConfigIsFrozen(unittest.TestCase):
    def test_the_policy_is_a_value_like_the_retry_policy(self):
        policy = dataclasses.replace(TODAY, max_days=180)
        self.assertEqual((policy.default_days, policy.max_days, policy.allow_no_expiry), (90, 180, True))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.max_days = 10


if __name__ == "__main__":
    unittest.main()
