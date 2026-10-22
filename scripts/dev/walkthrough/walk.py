#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The end-to-end walkthrough: Inkcast driven the way a person and their MCP client use it, in order, with every
step recorded (time, verdict, detail, the call and the reply) and pictured.

Both apps run for real in development mode (password sign-in) against the fake DEV and Medium APIs from
tests/fakes.py. The MCP side is the real SDK client (OAuthClientProvider: discovery, dynamic registration, PKCE,
the redirect opened in a Playwright browser, the code received by a real local listener on the callback port),
exactly the flow an MCP client such as a coding assistant performs. REST steps are what the GitHub Action and
the CLI send. Every MCP and REST step gets a rendered panel (the call and the reply, plainly labelled); every
browser step gets a screenshot. Each step asserts what it expects of the reply; a failed expectation fails the
step, and a page with a JavaScript error, an HTTP error status or a visible error is refused instead of pictured.

    WALK_PASSWORD=<at least 12 characters> walk.py --out <dir>

Ports come from the environment (WALK_INKCAST_PORT 8787, WALK_FAKES_PORT 8999, WALK_METRICS_PORT 9090,
WALK_CALLBACK_PORT 53682). Generated credentials are masked in the pictures and in results.json (prefix and an
ellipsis); the password is never written anywhere. Exit 0 only if every step passed; results.json is written
either way.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.server
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx2
from playwright.async_api import async_playwright

from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

from walklib import (  # noqa: E402
    counts, mask, mcp_panel_title, now, panel_html, pretty, render_call, rest_panel_title, slug,
)

CLIENT_NAME = "Walkthrough MCP client"
DEV_KEY = "devto-key"          # the fake DEV accepts exactly this key (tests/fakes.py)
FOREM_ACCEPT = "application/vnd.forem.api-v1+json"
SOURCE = "github.com/acme/blog"
SYNC_PATH = "posts/hello-from-a-repository.md"

DRAFT_STORY = """---
title: Publishing from an MCP client
subtitle: One markdown file, every platform, no copy and paste
tags: [writing, publishing, markdown, mcp]
canonical_url: https://blog.example.com/publishing-from-an-mcp-client
platforms: [devto]
---

# Publishing from an MCP client

You write the story once, in markdown, and your assistant publishes it where you asked.
Inkcast keeps the credentials; the assistant only ever sees the link.

## What happens

1. A dry run shows you each platform's version.
2. A draft lets you check it on the platform before it goes live.
"""

SCHEDULED_STORY = """---
title: A story for later this hour
tags: [scheduling]
platforms: [devto]
---

# A story for later this hour

Scheduled from the MCP client, then cancelled before it went out.
"""

SYNC_STORY = """---
title: Hello from a repository
subtitle: Synced by CI with a personal access token
tags: [git, ci, publishing]
canonical_url: https://blog.example.com/hello-from-a-repository
platforms: [devto]
---

# Hello from a repository

This file lives in a git repository. The GitHub Action sends it to Inkcast on every push; the ledger
remembers what it became, so the next push updates the post instead of duplicating it.
"""


class Callback:
    """What an MCP client's local listener does: receive the OAuth redirect on the callback port."""

    def __init__(self, port: int):
        self.port = port
        self.url = f"http://localhost:{port}/callback"
        self.received: list[str] = []
        self.event = threading.Event()
        walk = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                walk.received.append(f"http://localhost:{port}{self.path}")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<!doctype html><meta charset=utf-8><p style='font:16px system-ui;margin:40px'>"
                                 b"Signed in. You can return to your MCP client.</p>")
                walk.event.set()

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    async def wait(self, timeout: float = 30) -> str:
        deadline = time.monotonic() + timeout
        while not self.event.is_set():
            if time.monotonic() > deadline:
                raise TimeoutError("the local callback listener never received the redirect")
            await asyncio.sleep(0.05)
        return self.received[-1]

    def close(self) -> None:
        self.server.shutdown()


class Memory:
    def __init__(self):
        self.tokens = self.client = None

    async def get_tokens(self): return self.tokens
    async def set_tokens(self, t): self.tokens = t
    async def get_client_info(self): return self.client
    async def set_client_info(self, c): self.client = c


class Walk:
    """The step recorder: numbered pictures, one row per step in results.json, masking on the way in."""

    def __init__(self, page, panel_page, out: pathlib.Path, hidden: list[str]):
        self.page, self.panel_page, self.out, self.hidden = page, panel_page, out, hidden
        self.steps: list[dict] = []
        self.errors: list[str] = []
        self.phase = ""
        self.n = 0
        self.last_status: int | None = None
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
        except Exception:  # noqa: BLE001 — a detached frame during navigation is not a finding
            pass

    def _name(self, text: str) -> str:
        self.n += 1
        return f"{self.n:02d}-{slug(text)}.png"

    async def shot(self, text: str, full: bool = True) -> str:
        name = self._name(text)
        await self.page.mouse.move(0, 0)
        await self.page.evaluate("document.activeElement && document.activeElement.blur()")
        await self.page.wait_for_timeout(150)
        await self.page.screenshot(path=str(self.out / name), full_page=full)
        return name

    async def panel(self, kind: str, title: str, call: str, reply: str, narration: str, ok: bool) -> str:
        name = self._name(title)
        footer = f"Inkcast walkthrough, step {len(self.steps) + 1}, {now()}. Recorded from the live run; credentials masked."
        html = panel_html(kind, title, mask(call, self.hidden), mask(reply, self.hidden), narration, ok, footer)
        await self.panel_page.set_content(html, wait_until="load")
        await self.panel_page.screenshot(path=str(self.out / name), full_page=True)
        return name

    def record(self, step: str, kind: str, ok: bool, detail: str, narration: str = "", screenshot: str | None = None,
               **extra) -> dict:
        row = {"n": len(self.steps) + 1, "t": now(), "phase": self.phase, "step": step, "kind": kind, "ok": ok,
               "detail": mask(detail, self.hidden), "narration": narration, "screenshot": screenshot}
        row.update(mask(extra, self.hidden))
        self.steps.append(row)
        print(f"  [{'ok ' if ok else 'FAIL'}] {row['detail']}" + (f"  -> {screenshot}" if screenshot else ""))
        return row

    async def page_clean(self) -> str | None:
        """Why this page must not be pictured as a success, or None."""
        if self.errors:
            return f"JavaScript error on the page: {self.errors[0]}"
        if self.last_status and self.last_status >= 400:
            return f"the page answered HTTP {self.last_status}"
        err = self.page.locator(".err, [role=alert]")
        for i in range(min(await err.count(), 5)):
            if await err.nth(i).is_visible():
                text = (await err.nth(i).inner_text()).strip()
                if text:
                    return f"the page shows an error: {text[:160]}"
        h1 = self.page.locator("h1")
        if await h1.count():
            heading = (await h1.first.inner_text()).strip()
            if heading.lower().startswith(("error", "can't", "invalid", "request rejected", "not found")):
                return f"the page's heading is an error: {heading}"
        return None

    async def browser(self, step: str, narration: str, expect: tuple[str, Callable[[str], bool]] | None = None,
                      full: bool = True, **extra) -> bool:
        """Picture the current page if it is clean and (optionally) its text satisfies ``expect``."""
        await self.page.wait_for_load_state("networkidle")
        problem = await self.page_clean()
        text = await self.page.locator("body").inner_text()
        if problem is None and expect and not satisfied(expect[1], text):
            problem = f"expected {expect[0]}"
        shot = await self.shot(step if problem is None else f"failed-{step}", full)
        heading = " ".join((await self.page.locator("h1").first.inner_text()).split()) if await self.page.locator("h1").count() else ""
        self.record(step, "browser", problem is None,
                    problem or f"{self.page.url} — {heading or 'rendered'}" + (f"; {expect[0]}" if expect else ""),
                    narration, shot, url=self.page.url, checked=expect[0] if expect else None, **extra)
        self.errors.clear()
        return problem is None

    async def mcp(self, session: ClientSession, tool: str, arguments: dict, narration: str,
                  expect: tuple[str, Callable[[str], bool]]) -> str:
        """One tool call through the live MCP session: the reply text is checked, pictured and recorded."""
        try:
            r = await session.call_tool(tool, arguments)
            text = "\n".join(c.text for c in r.content if hasattr(c, "text"))
            is_error = bool(r.is_error)
        except Exception as exc:  # noqa: BLE001 — a transport failure is a failed step, not a crash
            text, is_error = f"{type(exc).__name__}: {exc}", True
        ok = not is_error and satisfied(expect[1], text)
        detail = f"{tool}: " + ("tool error — " if is_error else "") + (expect[0] if ok else f"expected {expect[0]}")
        shot = await self.panel("Model Context Protocol, tools/call", mcp_panel_title(tool), render_call(tool, arguments),
                                ("ERROR: " if is_error else "") + text, narration, ok)
        self.record(f"MCP {tool}", "mcp", ok, detail, narration, shot, call={"tool": tool, "arguments": arguments},
                    reply=text, is_error=is_error, checked=expect[0])
        return text

    async def rest(self, client: httpx2.AsyncClient, method: str, url: str, narration: str,
                   expect: tuple[str, Callable[[int, Any], bool]], *, label: str | None = None,
                   json_body: Any = None, headers: dict | None = None, auth_note: str = "") -> tuple[int, Any]:
        """One HTTP request as a client sends it: status and body checked, pictured and recorded."""
        path = urlparse(url).path + (f"?{urlparse(url).query}" if urlparse(url).query else "")
        shown_headers = {k: v for k, v in (headers or {}).items()}
        try:
            r = await client.request(method, url, json=json_body, headers=headers)
            status, text = r.status_code, r.text
            try:
                body: Any = r.json()
            except ValueError:
                body = text
        except httpx2.HTTPError as exc:
            status, body = 0, f"{type(exc).__name__}: {exc}"
        ok = satisfied(expect[1], status, body)
        call_lines = [f"{method.upper()} {url}"] + [f"{k}: {v}" for k, v in shown_headers.items()]
        if auth_note:
            call_lines.append(auth_note)
        call = "\n".join(call_lines) + (f"\n\n{pretty(json_body)}" if json_body is not None else "")
        reply = f"HTTP {status}\n\n{pretty(body)}"
        title = rest_panel_title(method, label or path)
        shot = await self.panel("HTTP request and response", title, call, reply, narration, ok)
        self.record(title, "rest", ok, f"HTTP {status}; " + (expect[0] if ok else f"expected {expect[0]}"), narration, shot,
                    call={"method": method.upper(), "url": url, "headers": shown_headers, "body": json_body},
                    reply={"status": status, "body": body}, checked=expect[0])
        return status, body


async def wait_http(url: str, want: int, procs: list[subprocess.Popen], timeout: float = 60) -> None:
    """Until ``url`` answers ``want``; at once if one of the processes has already exited (a port in use, a bad config)."""
    deadline = time.monotonic() + timeout
    async with httpx2.AsyncClient(timeout=2) as c:
        while time.monotonic() < deadline:
            try:
                if (await c.get(url)).status_code == want:
                    return
            except httpx2.HTTPError:
                pass
            gone = [p for p in procs if p.poll() is not None]
            if gone:
                raise RuntimeError(f"a server process exited with code {gone[0].returncode} before serving; see the logs in the output directory")
            await asyncio.sleep(0.3)
    raise TimeoutError(f"{url} did not answer {want} within {timeout:.0f} s")


def start_stack(out: pathlib.Path, state: pathlib.Path, base: str, fakes: str, ports: dict[str, int],
                password: str) -> list[subprocess.Popen]:
    """The fake platforms and Inkcast (every service in one process), as a developer runs them locally, the password
    and the secret key handed over in the child's environment as a developer does (Inkcast's posture check notes
    it; development mode allows it). ``state`` is a private directory outside the output: the database lives there,
    not in what CI uploads."""
    env = {k: v for k, v in os.environ.items() if k != "WALK_PASSWORD"}
    env.update({"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'tests'}",
                "INKCAST_ENV": "development", "INKCAST_LOGIN": "password", "INKCAST_PASSWORD": password,
                "INKCAST_SECRET_KEY": os.environ.get("INKCAST_SECRET_KEY") or secrets.token_urlsafe(48),
                "INKCAST_PUBLIC_URL": base, "INKCAST_PORT": str(ports["inkcast"]), "INKCAST_METRICS_PORT": str(ports["metrics"]),
                "INKCAST_DATABASE": str(state / "inkcast.db"),
                "INKCAST_API_URL_DEVTO": f"{fakes}/devto/api", "INKCAST_API_URL_MEDIUM": f"{fakes}/medium/v1",
                "INKCAST_INSIGHTS_INTERVAL": "1", "INKCAST_SCHEDULER_INTERVAL": "5", "INKCAST_LOG_FORMAT": "text",
                "FAKES_PORT": str(ports["fakes"]), "FAKES_HOST": "127.0.0.1", "FAKES_PUBLIC_URL": fakes})
    fakes_log = open(out / "fakes.log", "w")  # noqa: SIM115 — closed with the process
    server_log = open(out / "server.log", "w")  # noqa: SIM115
    return [
        subprocess.Popen([sys.executable, str(ROOT / "scripts" / "dev" / "fake_platforms.py")], env=env,
                         stdout=fakes_log, stderr=subprocess.STDOUT),
        subprocess.Popen([sys.executable, "-m", "inkcast.launcher", "all"], env=env, cwd=str(ROOT),
                         stdout=server_log, stderr=subprocess.STDOUT),
    ]


def satisfied(predicate: Callable[..., bool], *args) -> bool:
    """An expectation that raises (a body of the wrong shape) is simply not met."""
    try:
        return bool(predicate(*args))
    except Exception:  # noqa: BLE001
        return False


def has(*needles: str) -> Callable[[str], bool]:
    return lambda text: all(n in text for n in needles)


def story_title(markdown: str) -> str:
    return next(line.split(":", 1)[1].strip() for line in markdown.splitlines() if line.startswith("title:"))


async def run(args, password: str) -> dict:
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ports = {"inkcast": int(os.environ.get("WALK_INKCAST_PORT", "8787")), "fakes": int(os.environ.get("WALK_FAKES_PORT", "8999")),
             "metrics": int(os.environ.get("WALK_METRICS_PORT", "9090")), "callback": int(os.environ.get("WALK_CALLBACK_PORT", "53682"))}
    base, fakes = f"http://localhost:{ports['inkcast']}", f"http://127.0.0.1:{ports['fakes']}"
    started = now()
    state = pathlib.Path(tempfile.mkdtemp(prefix="inkcast-walk-"))
    procs = start_stack(out, state, base, fakes, ports, password)
    facts: dict[str, Any] = {"started": started, "base": base, "fakes": fakes, "client_name": CLIENT_NAME,
                             "callback": f"http://localhost:{ports['callback']}/callback",
                             "viewport": {"width": 820, "height": 560, "device_scale_factor": 2, "api_docs": {"width": 1280, "height": 900}}, "steps": []}
    hidden = [DEV_KEY]   # the fake's fixed key, masked in the pictures out of habit; the real credentials are masked by shape
    callback = Callback(ports["callback"])
    try:
        try:
            await wait_http(f"{fakes}/devto/api/users/me", 401, procs)
            await wait_http(f"{base}/livez", 200, procs)
        except Exception as exc:  # noqa: BLE001 — recorded as the failed first step, so the document says why nothing else ran
            facts["steps"] = [{"n": 1, "t": now(), "phase": "0. Start the stack", "step": "start Inkcast and the fake platforms",
                               "kind": "check", "ok": False, "detail": f"{type(exc).__name__}: {exc}", "narration": "", "screenshot": None}]
            raise SystemExit(1) from None
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context(viewport={"width": 820, "height": 560}, device_scale_factor=2,
                                                locale="en-US", timezone_id="UTC", color_scheme="light")
            page = await context.new_page()
            page.set_default_timeout(20_000)
            panel_page = await (await browser.new_context(viewport={"width": 1000, "height": 500}, device_scale_factor=2,
                                                           color_scheme="light")).new_page()
            w = Walk(page, panel_page, out, hidden)
            facts["steps"] = w.steps
            try:
                await journey(w, base, fakes, callback, facts, password)
            except BaseException as exc:  # noqa: BLE001 — the run must still leave its evidence (an ExceptionGroup is one)
                (out / "walk-error.txt").write_text("".join(traceback.format_exception(exc)))
                inner = getattr(exc, "exceptions", None) or [exc]
                shot = None
                try:
                    shot = await w.shot("failed-walk-aborted")
                except Exception:  # noqa: BLE001
                    pass
                w.record("walk aborted", "check", False,
                         "; ".join(f"{type(e).__name__}: {e}" for e in inner)[:600] + " (walk-error.txt has the traceback)", "", shot)
            await browser.close()
    finally:
        callback.close()
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(state, ignore_errors=True)
        facts["finished"] = now()
        facts.update(counts(facts))
        (out / "results.json").write_text(json.dumps(facts, indent=2, default=str))
    return facts


async def journey(w: Walk, base: str, fakes: str, callback: Callback, facts: dict, password: str) -> None:
    page = w.page
    storage = Memory()
    api = httpx2.AsyncClient(timeout=30)

    w.phase = "1. Add Inkcast to an MCP client"
    # ---- 1. Add Inkcast to an MCP client -----------------------------------------------------------------------
    await w.rest(api, "GET", f"{base}/.well-known/oauth-protected-resource/mcp",
                 "The client asks the MCP endpoint who protects it: Inkcast's own authorization server.",
                 ("the resource names its authorization server", lambda s, b: s == 200 and base in str(b.get("authorization_servers"))))
    await w.rest(api, "GET", f"{base}/.well-known/oauth-authorization-server",
                 "Discovery: where to register, authorize and get tokens; PKCE S256 is required.",
                 ("metadata lists registration, authorization and token endpoints with S256",
                  lambda s, b: s == 200 and all(k in b for k in ("registration_endpoint", "authorization_endpoint", "token_endpoint"))
                  and "S256" in b.get("code_challenge_methods_supported", [])))

    async def redirect_handler(url: str) -> None:
        client = await storage.get_client_info()
        info = client.model_dump(exclude_none=True) if client else {}
        w.record("dynamic client registration", "mcp", bool(info.get("client_id")),
                 f"registered as “{info.get('client_name')}” (client_id {info.get('client_id')}), redirect {callback.url}"
                 if info else "no client information after registration",
                 "The client registered itself (RFC 7591); no secret, PKCE instead.",
                 await w.panel("MCP client, OAuth", "Dynamic client registration", pretty({
                     "client_name": CLIENT_NAME, "redirect_uris": [callback.url], "grant_types": ["authorization_code", "refresh_token"],
                     "response_types": ["code"], "token_endpoint_auth_method": "none"}), pretty({k: v for k, v in info.items() if "secret" not in k}),
                     "The client registered itself (RFC 7591); no secret, PKCE instead.", bool(info.get("client_id"))),
                 reply={k: v for k, v in info.items() if "secret" not in k})
        await page.goto(url)
        await page.wait_for_selector("input[type=password]")
        await w.browser("sign-in page", "The client opened the authorization URL in the browser; Inkcast asks the person to sign in.",
                        ("the sign-in form", has("Sign in", "Password")))
        await page.fill("input[type=password]", password)
        async with page.expect_navigation():
            await page.keyboard.press("Enter")
        await page.wait_for_selector("button[value=approve]")
        await w.browser("consent page", "Signed in: Inkcast asks whether this client may use the account, naming the client.",
                        ("consent names the client", has(f"Allow {CLIENT_NAME} to use Inkcast?", "Approve")))
        async with page.expect_navigation():
            await page.click("button[value=approve]")

    async def callback_handler() -> AuthorizationCodeResult:
        url = await callback.wait()
        q = parse_qs(urlparse(url).query)
        code, state = q.get("code", [None])[0], q.get("state", [None])[0]
        await page.wait_for_url("**/callback*")
        w.record("authorization code at the local callback", "check", bool(code),
                 f"the redirect reached {callback.url} with a code and state" if code else f"no code in the redirect: {url}",
                 "The browser was sent back to the client's local listener with a one-time code; the client exchanges it with "
                 "its PKCE verifier.", None, reply={"code": code, "state": state})
        return AuthorizationCodeResult(code=code or "", state=state)

    auth = OAuthClientProvider(
        server_url=f"{base}/mcp",
        client_metadata=OAuthClientMetadata(client_name=CLIENT_NAME, redirect_uris=[callback.url],
                                            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                                            token_endpoint_auth_method="none"),
        storage=storage, redirect_handler=redirect_handler, callback_handler=callback_handler)

    async with httpx2.AsyncClient(auth=auth, timeout=30) as http:
        async with streamable_http_client(f"{base}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tokens = storage.tokens
                token_info = tokens.model_dump(exclude_none=True) if tokens else {}
                w.record("token exchange", "mcp", bool(token_info.get("access_token")),
                         f"access token issued, expires in {token_info.get('expires_in')} s, scope: {token_info.get('scope')}"
                         if token_info else "no tokens after the exchange",
                         "The code and verifier became an access token and a refresh token; the client stores them.",
                         await w.panel("MCP client, OAuth", "Token exchange",
                                       "POST /token\ngrant_type=authorization_code&code=…&code_verifier=…&redirect_uri=" + callback.url,
                                       pretty(token_info), "The code and verifier became an access token and a refresh token; "
                                       "the client stores them.", bool(token_info.get("access_token"))), reply=token_info)
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                server = init.server_info
                w.record("MCP initialize", "mcp", "inkcast_publish" in names,
                         f"server “{server.name}” {server.version}; {len(names)} tools: {', '.join(names)}",
                         "The session is open: the server introduces itself and lists its tools.",
                         await w.panel("MCP session", "initialize and tools/list", "initialize()\ntools/list()",
                                       pretty({"server": {"name": server.name, "version": server.version},
                                               "instructions": (init.instructions or "").strip(), "tools": names}),
                                       "The session is open: the server introduces itself and lists its tools.", "inkcast_publish" in names),
                         reply={"server": server.name, "version": server.version, "tools": names})
                facts["oauth"] = {"scope": token_info.get("scope"), "expires_in": token_info.get("expires_in")}
                access = token_info.get("access_token", "")
                bearer = {"Authorization": f"Bearer {access}"}
                facts["server"] = {"name": server.name, "version": server.version, "tools": names}

                w.phase = "2. Platforms, connect DEV, destinations"
                # ---- 2–4. Platforms, connect DEV, destinations ----------------------------------------------
                await w.mcp(session, "inkcast_platforms", {}, "Nothing is connected yet.",
                            ("DEV and Medium listed, both not connected", has("`devto`", "`medium`", "not connected")))
                link_text = await w.mcp(session, "inkcast_connect", {"platform_id": "devto"},
                                        "The assistant gets a one-time link for the person; the key never goes through chat.",
                                        ("a one-time /connect/ link", has(f"{base}/connect/", "one use")))
                link = next((t for t in link_text.split() if t.startswith(f"{base}/connect/")), None)
                if link:
                    await page.goto(link)
                    await page.wait_for_selector("input[name=api_key]")
                    await w.browser("connect page", "The person opens the link: a form for the DEV API key, with the help text.",
                                    ("the DEV connect form", has("Connect DEV", "API key")))
                    await page.fill("input[name=api_key]", DEV_KEY)
                    async with page.expect_navigation():
                        await page.keyboard.press("Enter")
                    await w.browser("dev connected", "Inkcast verified the key with DEV and stored it; the link is used up.",
                                    ("the page says DEV connected as ada", has("DEV connected", "ada")))
                await w.mcp(session, "inkcast_platforms", {}, "Back in the client: DEV is now connected.",
                            ("DEV connected as ada", has("`devto`", "connected as ada")))
                await w.mcp(session, "inkcast_destinations", {"platform_id": "devto"},
                            "Where else DEV can post: organizations, which DEV's API cannot list, so the reply says how to name one.",
                            ("the reply explains organizations", has("organization")))

                w.phase = "3. Preview, publish a draft, list posts"
                # ---- 5. Preview, publish a draft, list posts ----------------------------------------------
                await w.mcp(session, "inkcast_publish", {"markdown": DRAFT_STORY, "dry_run": True},
                            "A dry run first: DEV's version of the story, with any warnings, and nothing sent.",
                            ("a dry-run preview for DEV", has("DRY RUN", "[DEV]", "title: " + story_title(DRAFT_STORY))))
                draft = await w.mcp(session, "inkcast_publish", {"markdown": DRAFT_STORY, "draft": True},
                                    "Then the real thing, as a draft on DEV so it can be checked there first.",
                                    ("a DEV draft with its URL and post id", has("devto: draft", "https://", "(post `pst_")))
                facts["draft"] = {"title": story_title(DRAFT_STORY), "markdown": DRAFT_STORY,
                                  "post_id": next((t.strip("`().") for t in draft.split() if t.startswith("`pst_")), None),
                                  "url": next((t for t in draft.split() if t.startswith("https://")), None)}
                await w.mcp(session, "inkcast_posts", {}, "The post list shows the draft.",
                            ("the draft is listed", has("[draft]", story_title(DRAFT_STORY))))

                w.phase = "4. Schedule a post, then cancel it"
                # ---- 6. Schedule, list, cancel, list ----------------------------------------------------------
                when = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
                scheduled = await w.mcp(session, "inkcast_publish", {"markdown": SCHEDULED_STORY, "publish_at": when.isoformat()},
                                        "A second story scheduled an hour ahead (Inkcast refuses anything under two minutes away).",
                                        ("scheduled for the requested time", has("devto: scheduled for", when.isoformat()[:16])))
                post_id = next((t.strip("`().") for t in scheduled.split() if t.startswith("`pst_")), None)
                facts["scheduled"] = {"title": story_title(SCHEDULED_STORY), "publish_at": when.isoformat(), "post_id": post_id}
                await w.mcp(session, "inkcast_posts", {}, "Listed as scheduled, next to the draft.",
                            ("the scheduled post is listed", has("[scheduled]", story_title(SCHEDULED_STORY))))
                await w.mcp(session, "inkcast_cancel", {"post_id": post_id or "pst_missing"}, "Second thoughts: cancel it before it goes out.",
                            ("the cancellation names the post", has("Cancelled", story_title(SCHEDULED_STORY))))
                await w.mcp(session, "inkcast_posts", {}, "Now listed as cancelled; nothing reached DEV.",
                            ("the post is listed as cancelled", has("[cancelled]", story_title(SCHEDULED_STORY))))

                w.phase = "5. A personal access token for CI"
                # ---- 7. A personal access token for CI --------------------------------------------------------
                token_text = await w.mcp(session, "inkcast_create_token", {"name": "blog repo CI", "scopes": ["stories:sync"], "expires_in_days": 90},
                                         "For CI the person needs a token the assistant never sees: another link to open.",
                                         ("a /tokens/new link with the name and scope", has(f"{base}/tokens/new?", "INKCAST_TOKEN")))
                token_link = next((t for t in token_text.split() if t.startswith(f"{base}/tokens/new?")), None)
                pat = ""
                if token_link:
                    await page.goto(token_link)
                    await page.wait_for_selector("input[type=password]")
                    await page.fill("input[type=password]", password)
                    async with page.expect_navigation():
                        await page.keyboard.press("Enter")
                    await page.wait_for_selector("button[value=approve]")
                    await w.browser("token confirm", "After signing in again, the person confirms the token's name, scope and expiry.",
                                    ("the confirmation names the token and scope", has("Create a personal access token?", "blog repo CI", "stories:sync")))
                    async with page.expect_navigation():
                        await page.click("button[value=approve]")
                    await page.wait_for_selector("#token")
                    pat = (await page.locator("#token").inner_text()).strip()
                    await page.evaluate("(m) => { document.getElementById('token').textContent = m; }", mask(pat))
                    await w.browser("token created", "Shown once, to be stored as the CI secret INKCAST_TOKEN (masked here).",
                                    ("the token page, with the token masked", has("Token created", "INKCAST_TOKEN", "ink_pat_")))
                facts["pat_prefix"] = mask(pat) if pat else None

    w.phase = "6. Git sync, as the GitHub Action does it"
    # ---- 8. Git sync as the GitHub Action does ----------------------------------------------------------------
    sync_body = {"source": SOURCE, "ref": "refs/heads/main", "platforms": None, "dry_run": False,
                 "files": [{"path": SYNC_PATH, "markdown": SYNC_STORY,
                            "image_base_url": f"https://raw.githubusercontent.com/acme/blog/main/{SYNC_PATH.rsplit('/', 1)[0]}/"}]}
    key = hashlib.sha256(json.dumps(sync_body, sort_keys=True).encode()).hexdigest()
    status, synced = await w.rest(api, "POST", f"{base}/api/v1/stories:sync",
                                  "What the GitHub Action sends on a push: the file, its path and the repository id, with the "
                                  "personal access token and an Idempotency-Key so a retried job can never publish twice.",
                                  ("the file was created on DEV with a URL", lambda s, b: s == 200 and isinstance(b, dict)
                                   and b.get("results", [{}])[0].get("action") == "create" and b["results"][0].get("platform") == "devto"
                                   and bool(b["results"][0].get("url"))),
                                  json_body=sync_body, headers={"Authorization": f"Bearer {pat}", "Idempotency-Key": key,
                                                                "User-Agent": "inkcast-sync"})
    facts["sync"] = {"source": SOURCE, "path": SYNC_PATH, "markdown": SYNC_STORY, "title": story_title(SYNC_STORY),
                     "result": synced.get("results", [{}])[0] if isinstance(synced, dict) else None}
    status, ledger = await w.rest(api, "GET", f"{base}/api/v1/ledger?source={SOURCE}",
                                  "The ledger: what each file became on each platform, keyed by the hash of what was sent.",
                                  ("the ledger has the file, published, with a content hash", lambda s, b: s == 200 and isinstance(b, list)
                                   and any(e.get("path") == SYNC_PATH and e.get("platform") == "devto" and e.get("content_hash") for e in b)),
                                  headers=bearer, auth_note="(the MCP session's OAuth access token)")
    facts["ledger"] = [e for e in ledger if isinstance(e, dict)] if isinstance(ledger, list) else []

    w.phase = "7. Insights"
    # ---- 9. Insights ----------------------------------------------------------------------------------------
    deadline, polls, summary, settled = time.monotonic() + 30, 0, None, False
    while time.monotonic() < deadline and not settled:
        polls += 1
        r = await api.get(f"{base}/api/v1/insights/summary?days=7", headers=bearer)
        summary = r.json() if r.status_code == 200 else None
        settled = bool(summary) and summary.get("sync_by_action", {}).get("create", 0) >= 1 \
            and summary.get("posts_by_status", {}).get("cancelled", {}).get("devto", 0) >= 1
        if not settled:
            await asyncio.sleep(1)
    await w.rest(api, "GET", f"{base}/api/v1/insights/summary?days=7",
                 f"The person's own numbers, folded from domain events by the insights worker (settled after {polls} poll(s)): "
                 "posts by status, the ledger, the sync outcomes, publish success and on-time rates.",
                 ("the draft, the cancellation and the sync are all counted", lambda s, b: s == 200 and isinstance(b, dict)
                  and b.get("posts_by_status", {}).get("draft", {}).get("devto", 0) >= 1
                  and b.get("posts_by_status", {}).get("cancelled", {}).get("devto", 0) >= 1
                  and b.get("sync_by_action", {}).get("create", 0) >= 1 and b.get("ledger_by_status", {}).get("published", 0) >= 1),
                 headers=bearer, auth_note="(the MCP session's OAuth access token)")

    w.phase = "8. The API reference"
    # ---- 10. The API reference ------------------------------------------------------------------------------
    await page.set_viewport_size({"width": 1280, "height": 900})
    await page.goto(f"{base}/api")
    await page.wait_for_selector(".swagger-ui .opblock", timeout=20_000)
    await w.browser("api swagger", "The REST reference, served by every deployment with vendored assets (nothing from a CDN).",
                    ("Swagger UI lists the stories:sync route", has("Inkcast API", "/api/v1/stories:sync")), full=False)
    await page.goto(f"{base}/api/redoc")
    await page.wait_for_selector("h1:has-text('Inkcast API')", timeout=20_000)
    await page.locator("h2", has_text="Publish, schedule, draft or preview a story").first.wait_for(timeout=20_000)
    await w.browser("api redoc", "The same contract in ReDoc.", ("ReDoc renders the API title", has("Inkcast API")), full=False)

    w.phase = "9. What the platform received"
    # ---- What the platform received, read back from the fake DEV for the integrity check ------------------------
    status, articles = await w.rest(api, "GET", f"{fakes}/devto/api/articles/me/all",
                                    "Read back from the fake DEV, as the integrity check's witness: the draft and the synced file, "
                                    "and nothing for the cancelled schedule.",
                                    ("two articles: the draft (unpublished) and the synced file (published)", lambda s, b: s == 200
                                     and isinstance(b, list) and len(b) == 2
                                     and [a.get("published") for a in b] == [False, True]),
                                    label="/devto/api/articles/me/all (fake DEV)", headers={"api-key": DEV_KEY, "Accept": FOREM_ACCEPT})
    facts["dev_articles"] = articles if isinstance(articles, list) else []
    await api.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="directory for the pictures and results.json")
    args = ap.parse_args()
    password = os.environ.get("WALK_PASSWORD", "")
    if len(password) < 12:
        print("WALK_PASSWORD (at least 12 characters) is required in the environment; it is never an argument.", file=sys.stderr)
        return 2
    try:
        facts = asyncio.run(run(args, password))
    except SystemExit:
        print("the stack did not start; results.json records why", file=sys.stderr)
        return 1
    print(f"\n{facts['passed']} steps passed, {facts['failed']} failed -> {args.out}/results.json")
    return 0 if facts["failed"] == 0 and facts["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
