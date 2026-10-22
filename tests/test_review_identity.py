"""Adversarial review of identity and tokens (services/identity, libs/auth, libs/db/identity.py): each test forces a
case that used to break, and fails without the fix committed with it."""

import asyncio
import base64
import hashlib
import re
import unittest
from urllib.parse import parse_qs, urljoin, urlparse

import httpx2
from starlette.testclient import TestClient

import test_e2e
from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http.app import compose

PASSWORD = "p" * 12


def password_config(**overrides) -> Config:
    return Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="password",
                  password=PASSWORD, **overrides)


def in_process(names: str, cfg: Config):
    """(ASGI app, context) for these services, as the launcher builds them."""
    specs = resolve(names)
    ctx = build(cfg, specs)
    return compose(specs, ctx), ctx


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def form_fields(html: str) -> dict:
    return dict(re.findall(r'name=(\w+) value="([^"]*)"', html))


class ConsentBinding(test_e2e.Harness):
    """The consent page (and the token page) may only be answered by the browser that signed in. The flow's pending
    id travels in URLs (it is the OIDC ``state``), so whoever started a flow knows it: if that is enough to approve,
    an attacker who sends someone a sign-in link approves on their behalf the moment they arrive (with a live
    Keycloak session they never see a password prompt)."""

    VERIFIER = "attacker-verifier-" + "x" * 40

    async def _victim_reaches_consent(self, link: str) -> None:
        async with httpx2.AsyncClient(follow_redirects=True, timeout=20) as victim:   # follows, never clicks
            page = await victim.get(link)
        self.assertIn("/auth/consent", str(page.url))

    async def _attacker_approves(self, attacker: httpx2.AsyncClient, pending: str) -> httpx2.Response:
        base = self.config.public_url
        page = await attacker.get(f"{base}/auth/consent", params={"pending": pending})
        return await attacker.post(f"{base}/auth/consent", data={
            "pending": pending, "csrf": form_fields(page.text).get("csrf", ""), "decision": "approve"})

    def test_an_attacker_cannot_approve_the_oauth_consent_of_someone_who_followed_their_link(self):
        base, redirect = self.config.public_url, "https://attacker.example/cb"

        async def run():
            async with httpx2.AsyncClient(timeout=20) as attacker:
                client = (await attacker.post(f"{base}/register", json={
                    "client_name": "Claude Code", "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})).json()
                r = await attacker.get(f"{base}/authorize", params={
                    "client_id": client["client_id"], "response_type": "code", "redirect_uri": redirect, "state": "s",
                    "code_challenge": s256(self.VERIFIER), "code_challenge_method": "S256"})
                link = urljoin(base, r.headers["location"])                  # /auth/login?pending=...
                await self._victim_reaches_consent(link)
                r = await self._attacker_approves(attacker, parse_qs(urlparse(link).query)["pending"][0])
                code = parse_qs(urlparse(r.headers.get("location", "")).query).get("code", [None])[0]
                if not code:
                    return None
                token = (await attacker.post(f"{base}/token", data={
                    "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                    "client_id": client["client_id"], "code_verifier": self.VERIFIER})).json()
                me = await attacker.get(f"{base}/api/v1/me", headers={"Authorization": f"Bearer {token['access_token']}"})
                return me.status_code

        self.assertIsNone(asyncio.run(run()), "the attacker got an authorization code (and a token) for the victim")

    def test_an_attacker_cannot_approve_a_token_page_someone_else_signed_in_to(self):
        base = self.config.public_url

        async def run():
            async with httpx2.AsyncClient(timeout=20) as attacker:
                r = await attacker.get(f"{base}/tokens/new", params={"name": "ci", "scopes": "posts:write", "days": "366"})
                link = r.headers["location"]                                  # the IdP's URL, state = pending id
                await self._victim_reaches_consent(link)
                r = await self._attacker_approves(attacker, parse_qs(urlparse(link).query)["state"][0])
                return re.search(r"ink_pat_[A-Za-z0-9_\-]+", r.text)

        self.assertIsNone(asyncio.run(run()), "the attacker was shown a personal access token for the victim")

    def test_the_browser_that_signed_in_still_approves(self):
        browser = test_e2e.Browser()
        asyncio.run(browser(self._authorize_url()))
        self.assertTrue(browser.result.get("code"), browser.result)

    def _authorize_url(self) -> str:
        base = self.config.public_url
        client = httpx2.post(f"{base}/register", json={
            "client_name": "Claude Code", "redirect_uris": ["http://localhost:53682/callback"],
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"],
            "response_types": ["code"]}).json()
        return f"{base}/authorize?" + httpx2.QueryParams({
            "client_id": client["client_id"], "response_type": "code", "redirect_uri": "http://localhost:53682/callback",
            "state": "s", "code_challenge": s256("v" * 50), "code_challenge_method": "S256"}).__str__()


class ResourceIndicators(unittest.TestCase):
    """RFC 8707 / the MCP authorization spec: a token is for one audience. Inkcast's own resource servers don't check
    the audience (validate_token_resource=False), so the authorization server must not mint Inkcast tokens for another
    resource: a malicious MCP server that names Inkcast as its authorization server would otherwise receive a token
    the person's client got "for it", and replay it to Inkcast."""

    REDIRECT = "http://localhost:53682/callback"
    VERIFIER = "v" * 50

    def sign_in(self, c: TestClient, resource: str | None):
        """Register, authorize with ``resource``, sign in, approve, exchange; the /api/v1/me status with the token,
        or the authorization error."""
        client = c.post("/register", json={"client_name": "Claude Code", "redirect_uris": [self.REDIRECT],
                                           "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"],
                                           "response_types": ["code"]}).json()
        params = {"client_id": client["client_id"], "response_type": "code", "redirect_uri": self.REDIRECT,
                  "state": "s", "code_challenge": s256(self.VERIFIER), "code_challenge_method": "S256"}
        if resource is not None:
            params["resource"] = resource
        r = c.get("/authorize", params=params, follow_redirects=False)
        location = r.headers.get("location", "")
        if location.startswith(self.REDIRECT):
            return parse_qs(urlparse(location).query)["error"][0]
        pending = parse_qs(urlparse(location).query)["pending"][0]
        r = c.post("/auth/callback", data={"pending": pending, "password": PASSWORD}, follow_redirects=False)
        page = c.get(r.headers["location"])
        r = c.post("/auth/consent", data={**form_fields(page.text), "decision": "approve"}, follow_redirects=False)
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        token = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": self.REDIRECT,
                                       "client_id": client["client_id"], "code_verifier": self.VERIFIER}).json()
        return c.get("/api/v1/me", headers={"Authorization": f"Bearer {token['access_token']}"}).status_code

    def test_inkcast_mints_no_token_for_another_resource(self):
        cfg = password_config()
        app, _ = in_process("IdentityService,PublishingApi", cfg)
        with TestClient(app, base_url=cfg.public_url) as c:
            self.assertEqual(self.sign_in(c, "https://evil.example/mcp"), "invalid_target",
                             "a token minted for another server is accepted by Inkcast")
            self.assertEqual(self.sign_in(c, "https://localhost:8787.evil.example/mcp"), "invalid_target")
            # Its own resource, as advertised or spelled as clients do, and no resource at all, still work.
            for resource in (cfg.mcp_url, cfg.mcp_url + "/", "HTTP://LOCALHOST:8787/mcp", cfg.public_url, None):
                self.assertEqual(self.sign_in(c, resource), 200, resource)


class RevocationReachesRequestsInFlight(unittest.TestCase):
    """Revoking a token (or Keycloak cutting the person off) must stop what it can do. A REST request whose body is
    still arriving when the token is revoked must not act afterwards: the caller controls how slowly the body comes,
    so "checked at the first byte" is a window they can hold open for as long as they like."""

    def test_a_pat_cannot_be_minted_with_a_token_revoked_while_the_body_was_arriving(self):
        cfg = password_config()
        app, ctx = in_process("IdentityService", cfg)
        store = ctx.store
        user = store.upsert_user("local:owner", "owner", None, None)
        access = "ic_at_" + "a" * 43
        store.save_token(access, "access", "c1", user["id"], ["inkcast"], None, "family-1", 3600)

        async def run():
            parked, go = asyncio.Event(), asyncio.Event()

            async def slow_body():
                yield b'{"name": "keep-access", '
                parked.set()                                   # the handler is waiting for the rest
                await go.wait()
                yield b'"scopes": ["posts:write"], "expires_in_days": null}'

            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=cfg.public_url) as c:
                sent = asyncio.create_task(c.post("/api/v1/tokens", content=slow_body(), headers={
                    "Authorization": f"Bearer {access}", "Content-Type": "application/json"}))
                await parked.wait()
                store.revoke_family("family-1")                 # the person revokes the stolen token
                self.assertIsNone(ctx.verifier.principal(access))
                go.set()
                return await sent

        r = asyncio.run(run())
        self.assertEqual(store.list_api_tokens(user["id"]), [], "a never-expiring PAT outlived the revocation")
        self.assertEqual(r.status_code, 401)


class CutOffCoversSignInsInProgress(unittest.TestCase):
    """E9.F9.5.S2: a person Keycloak disables loses everything at once, and their old tokens stay dead when they are
    enabled again. A sign-in that already finished (consent page open, 15 minutes to answer) is a grant waiting to
    happen: answered after the cut-off it mints a PAT or code that the _NOT_DISABLED guard hides only until the
    person is enabled again, which is exactly when an account taken back from an attacker is handed back."""

    def test_a_consent_answered_after_the_cut_off_yields_nothing_once_re_enabled(self):
        cfg = password_config()
        app, ctx = in_process("IdentityService", cfg)
        store = ctx.store
        with TestClient(app, base_url=cfg.public_url) as c:
            form = c.get("/tokens/new", params={"name": "ci", "scopes": "posts:write", "days": "366"})
            pending = form_fields(form.text)["pending"]
            r = c.post("/auth/callback", data={"pending": pending, "password": PASSWORD}, follow_redirects=False)
            self.assertIn("/auth/consent", r.headers["location"])
            user_id = store.get_pending(pending)["user_id"]

            cut = store.disable_user(user_id)                          # Keycloak disabled them; the sync ran
            self.assertEqual(cut["personal_access_tokens"], 0)

            page = c.get(r.headers["location"])
            done = c.post("/auth/consent", data={**form_fields(page.text), "decision": "approve"})
            store.upsert_user("local:owner", "owner", None, None)      # enabled again, signs in
        minted = re.search(r"ink_pat_[A-Za-z0-9_\-]+", done.text)
        alive = minted and ctx.verifier.principal(minted.group(0))
        self.assertFalse(alive, "a PAT approved after the cut-off works once the person is enabled again")
        self.assertEqual(store.list_api_tokens(user_id), [])


class RateLimitsCannotBeSidestepped(unittest.TestCase):
    """The endpoints anyone can call without a token are limited per client (libs/http/ratelimit.py)."""

    FORM = {"Content-Type": "application/x-www-form-urlencoded"}

    def test_the_password_form_sent_as_a_get_is_limited_like_the_post(self):
        cfg = password_config(rate_limit_per_minute=2)
        app, _ = in_process("IdentityService", cfg)
        with TestClient(app, base_url=cfg.public_url) as c:
            pending = form_fields(c.get("/tokens/new", params={"name": "x", "scopes": "posts:read"}).text)["pending"]

            def guess(password: str) -> int:   # /auth/callback also takes GET (the IdP's redirect), and reads a form
                return c.request("GET", "/auth/callback", content=f"pending={pending}&password={password}",
                                 headers=self.FORM, follow_redirects=False).status_code

            self.assertEqual([guess("guess-1"), guess("guess-2")], [403, 403])
            self.assertEqual(guess(PASSWORD), 429, "password guesses sent as GET are never limited")

    def test_authorize_writes_a_row_per_request_so_it_is_limited(self):
        cfg = password_config(rate_limit_per_minute=2)
        app, ctx = in_process("IdentityService", cfg)
        with TestClient(app, base_url=cfg.public_url) as c:
            client = c.post("/register", json={"client_name": "x", "redirect_uris": ["http://localhost:1/cb"],
                                               "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"],
                                               "response_types": ["code"]}).json()
            params = {"client_id": client["client_id"], "response_type": "code", "redirect_uri": "http://localhost:1/cb",
                      "code_challenge": "x" * 43, "code_challenge_method": "S256"}
            codes = [c.get("/authorize", params=params, follow_redirects=False).status_code for _ in range(3)]
            codes.append(c.post("/authorize", data=params, follow_redirects=False).status_code)
        rows = ctx.store._one("SELECT count(*) AS n FROM pending_auth")["n"]
        self.assertEqual((codes, rows), ([302, 302, 429, 429], 2), "one registered client, unbounded pending rows")


class ConsentNamesTheRealHost(unittest.TestCase):
    """The consent page's one trustworthy signal is where approving sends the code ("Only approve if you just
    started this from your own tool"); the client name is whatever the registration said."""

    def test_userinfo_in_the_redirect_uri_cannot_pose_as_a_local_client(self):
        cfg = password_config()
        app, _ = in_process("IdentityService", cfg)
        redirect = "http://localhost:53682@evil.example/callback"         # the code goes to evil.example
        with TestClient(app, base_url=cfg.public_url) as c:
            client = c.post("/register", json={"client_name": "Claude Code", "redirect_uris": [redirect],
                                               "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"],
                                               "response_types": ["code"]}).json()
            r = c.get("/authorize", params={"client_id": client["client_id"], "response_type": "code",
                                            "redirect_uri": redirect, "code_challenge": "x" * 43,
                                            "code_challenge_method": "S256"}, follow_redirects=False)
            pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]
            r = c.post("/auth/callback", data={"pending": pending, "password": PASSWORD}, follow_redirects=False)
            page = c.get(r.headers["location"]).text
        shown = re.search(r"sends you back to <code>([^<]*)</code>", page).group(1)
        self.assertEqual(shown, "evil.example")


class NoBearerLinksInLogs(unittest.TestCase):
    """A connect ticket is a bearer secret for 15 minutes: whoever has /connect/<ticket> attaches platform
    credentials to that person's account. The access log keeps paths (never query strings, for OAuth codes)."""

    def test_the_access_log_never_holds_a_connect_ticket(self):
        import io
        import logging
        from inkcast.libs.observability import JsonFormatter, Redactor, TextFormatter
        cfg = password_config()
        app, ctx = in_process("ConnectorService", cfg)
        user = ctx.store.upsert_user("local:owner", "owner", None, None)
        logger = logging.getLogger("inkcast.http")
        for formatter in (JsonFormatter("connector-service"), TextFormatter("%(message)s")):
            ticket = ctx.store.create_ticket(user["id"], "devto")
            out = io.StringIO()
            handler = logging.StreamHandler(out)
            handler.setFormatter(formatter)
            handler.addFilter(Redactor())
            logger.addHandler(handler)
            level, logger.level = logger.level, logging.INFO
            try:
                with TestClient(app, base_url=cfg.public_url) as c:
                    self.assertEqual(c.get(f"/connect/{ticket}").status_code, 200)
            finally:
                logger.removeHandler(handler)
                logger.setLevel(level)
            self.assertIn("GET /connect/", out.getvalue())                      # the request is still logged
            self.assertNotIn(ticket, out.getvalue(), f"{type(formatter).__name__} wrote the ticket")


if __name__ == "__main__":
    unittest.main()
