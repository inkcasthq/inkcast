"""Inkcast: publish and schedule writing across platforms from Claude Code (or any MCP client)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import httpx2
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import __version__
from .auth import pages
from .auth.identity import IdentityProvider, LoginError, make_identity_provider
from .auth.provider import SCOPE, InkcastOAuthProvider
from .config import Config, load_config
from .platforms import Account, Platform, PlatformError, load_platforms
from .publisher import Outcome, Publisher
from .storage import Store
from .story import StoryError, parse_story

log = logging.getLogger("inkcast")

INSTRUCTIONS = """\
Inkcast publishes and schedules stories to writing platforms (see inkcast_platforms).

To publish a markdown file: read the file yourself and pass its FULL text as `markdown`
(this server may run remotely and cannot read local paths). YAML front matter (title,
subtitle, tags, canonical_url, cover_image, platforms, publish_at, and per-platform blocks
like `medium:` or `devto:`) is used automatically; explicit arguments override it.

Before the first real publish of a file, call inkcast_publish with dry_run=true and show the
person the per-platform preview and warnings. If a platform isn't connected, call
inkcast_connect and give the person the link; never ask them to paste API keys into chat.
"""


@dataclass
class App:
    config: Config
    store: Store
    http: httpx2.AsyncClient
    platforms: dict[str, Platform]
    publisher: Publisher
    oauth: InkcastOAuthProvider
    identity: IdentityProvider
    mcp: MCPServer


def _user_id() -> str:
    access = get_access_token()
    if access is None or not access.subject:
        raise ToolError("Not signed in. Run /mcp in Claude Code and authenticate Inkcast.")
    return access.subject


def _fmt_outcome(o: Outcome, publish_at: datetime | None) -> str:
    name = o.platform
    if o.status == "scheduled" and publish_at:
        line = f"- {name}: scheduled for {publish_at.isoformat()} (post `{o.post_id}`)"
    elif o.status in ("published", "draft", "unlisted"):
        line = f"- {name}: {o.status}" + (f" → {o.url}" if o.url else "") + f" (post `{o.post_id}`)"
    elif o.status == "skipped":
        line = f"- {name}: skipped — {o.message}"
    else:
        line = f"- {name}: FAILED — {o.message}"
    for note in o.notes:
        line += f"\n  note: {note}"
    if o.prepared:
        for w in o.prepared.warnings:
            line += f"\n  warning: {w}"
    return line


def build(config: Config, transport: httpx2.AsyncBaseTransport | None = None) -> App:
    store = Store(config.database_path, config.secret_key)
    http = httpx2.AsyncClient(timeout=30.0, transport=transport, follow_redirects=False)
    registry = load_platforms(http, config.platform_urls)
    publisher = Publisher(store, registry)
    oauth = InkcastOAuthProvider(config, store)
    identity = make_identity_provider(config, http)

    mcp = MCPServer(
        name="inkcast",
        title="Inkcast",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth_server_provider=oauth,
        auth=AuthSettings(
            issuer_url=config.public_url,
            resource_server_url=config.mcp_url,
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
            validate_token_resource=False,
        ),
    )
    app = App(config, store, http, registry, publisher, oauth, identity, mcp)

    def platform(pid: str) -> Platform:
        p = registry.get(pid.strip().lower())
        if not p:
            raise ToolError(f"Unknown platform {pid!r}. Available: {', '.join(registry)}.")
        return p

    # ---- tools ----------------------------------------------------------

    @mcp.tool(title="List platforms", annotations=ToolAnnotations(read_only_hint=True))
    async def inkcast_platforms() -> str:
        """List the publishing platforms Inkcast supports and which ones you have connected."""
        connected = store.connected_accounts(_user_id())
        lines = []
        for pid, p in registry.items():
            acct = connected.get(pid)
            state = f"connected as {acct.get('handle') or acct.get('name')}" if acct else "not connected"
            dest = f"; supports {p.destination_label}s" if p.destination_label else ""
            lines.append(f"- `{pid}` — {p.name}: {state}{dest}")
        return "Platforms:\n" + "\n".join(lines)

    @mcp.tool(title="Connect a platform", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    async def inkcast_connect(platform_id: Annotated[str, Field(description="Platform id from inkcast_platforms, e.g. 'devto'.")]) -> str:
        """Get a one-time link where the person enters this platform's credentials (never paste keys into chat)."""
        p = platform(platform_id)
        ticket = store.create_ticket(_user_id(), p.id)
        return (f"Open this link to connect {p.name} (valid 15 minutes, one use):\n{config.public_url}/connect/{ticket}\n\n"
                f"{p.connect_help}")

    @mcp.tool(title="Disconnect a platform", annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def inkcast_disconnect(platform_id: Annotated[str, Field(description="Platform id to disconnect.")]) -> str:
        """Delete the stored credentials for a platform. Scheduled posts to it will fail until it is reconnected."""
        p = platform(platform_id)
        removed = store.delete_credentials(_user_id(), p.id)
        return f"Disconnected {p.name}." if removed else f"{p.name} wasn't connected."

    @mcp.tool(title="List destinations", annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def inkcast_destinations(platform_id: Annotated[str, Field(description="Platform id, e.g. 'medium'.")]) -> str:
        """List where you can post on a platform besides your own profile (Medium publications, DEV organizations...)."""
        p = platform(platform_id)
        if not p.destination_label:
            return f"{p.name} has no destinations besides your own profile."
        connected = store.get_credentials(_user_id(), p.id)
        if not connected:
            raise ToolError(f"{p.name} isn't connected; call inkcast_connect first.")
        creds, acct = connected
        try:
            dests = await p.destinations(creds, Account.from_dict(acct))
        except PlatformError as exc:
            raise ToolError(str(exc)) from exc
        if not dests:
            return (f"{p.name} can't list {p.destination_label}s; pass the {p.destination_label}'s name or id as the "
                    f"destination and Inkcast will look it up when publishing.")
        return f"{p.name} {p.destination_label}s:\n" + "\n".join(
            f"- {d.name} (`{d.id}`)" + (f" — {d.url}" if d.url else "") for d in dests)

    @mcp.tool(
        title="Publish or schedule a story",
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False, open_world_hint=True),
    )
    async def inkcast_publish(
        markdown: Annotated[str, Field(description="Full markdown text of the story, including YAML front matter.")],
        platforms: Annotated[list[str] | None, Field(description="Platform ids to post to, e.g. ['medium','devto']. Defaults to front matter `platforms`.")] = None,
        title: Annotated[str | None, Field(description="Title. Defaults to front matter `title`, then the first `# Heading`.")] = None,
        subtitle: Annotated[str | None, Field(description="Subtitle / summary. Defaults to front matter `subtitle` or `description`.")] = None,
        tags: Annotated[list[str] | None, Field(description="Tags; each platform applies its own limits.")] = None,
        canonical_url: Annotated[str | None, Field(description="URL of the original post, set as the canonical link everywhere.")] = None,
        cover_image: Annotated[str | None, Field(description="Public URL of a cover image.")] = None,
        destinations: Annotated[dict[str, str] | None, Field(description="Per-platform destination, e.g. {'medium': 'Better Programming', 'devto': 'my-org'}.")] = None,
        publish_at: Annotated[str | None, Field(description="Schedule time, ISO 8601 WITH a UTC offset, e.g. 2026-10-21T09:00:00-05:00. Omit to publish now.")] = None,
        draft: Annotated[bool, Field(description="Create drafts on the platforms instead of publishing.")] = False,
        image_base_url: Annotated[str | None, Field(description="Base URL to turn relative image paths into absolute ones.")] = None,
        dry_run: Annotated[bool, Field(description="Validate and preview each platform's version; nothing is sent or saved.")] = False,
    ) -> str:
        """Publish, schedule or draft one markdown story to one or more platforms. Use dry_run=true first to preview."""
        user = _user_id()
        try:
            story = parse_story(
                markdown, known_platforms=registry.keys(), title=title, subtitle=subtitle, tags=tags,
                canonical_url=canonical_url, cover_image=cover_image, platforms=platforms, destinations=destinations,
                publish_at=publish_at, draft=draft, image_base_url=image_base_url,
            )
        except StoryError as exc:
            raise ToolError(str(exc)) from exc
        targets = story.platforms or [pid for pid in store.connected_accounts(user) if pid in registry]
        if not targets:
            raise ToolError("Say which platforms to post to (e.g. platforms=['devto']) or connect one with inkcast_connect.")

        when = story.publish_at
        head = [f"“{story.title}”" + (f" — {story.subtitle}" if story.subtitle else "")]
        head += [f"warning: {w}" for w in story.warnings]

        if dry_run:
            ready, problems = publisher.prepare(story, targets)
            connected = store.connected_accounts(user)
            blocks = ["DRY RUN — nothing was sent.", *head,
                      f"Publishes: {'at ' + when.isoformat() if when else ('as drafts' if draft else 'immediately')}"]
            for prep in ready:
                p = registry[prep.platform]
                block = [f"\n[{p.name}]" + ("" if prep.platform in connected else " — NOT CONNECTED")]
                block += [f"  {k}: {v}" for k, v in prep.preview.items()]
                block += [f"  warning: {w}" for w in prep.warnings]
                if prep.platform not in connected and (help_text := p.not_connected_help(story.for_platform(p.id))):
                    block.append(f"  {help_text}")
                blocks.append("\n".join(block))
            blocks += [f"\n[{registry[o.platform].name}] FAILED — {o.message}" for o in problems]
            return "\n".join(blocks)

        outcomes = await publisher.publish(user, story, targets)
        return "\n".join(head + [_fmt_outcome(o, when) for o in outcomes])

    @mcp.tool(title="List posts", annotations=ToolAnnotations(read_only_hint=True))
    async def inkcast_posts(
        days_back: Annotated[int, Field(ge=0, le=366, description="Include posts from this many days ago.")] = 7,
        days_ahead: Annotated[int, Field(ge=0, le=730, description="Include posts scheduled up to this many days ahead.")] = 90,
    ) -> str:
        """List your scheduled, published, draft, failed and cancelled posts."""
        now = datetime.now(timezone.utc)
        rows = store.list_posts(_user_id(), (now - timedelta(days=days_back)).timestamp(), (now + timedelta(days=days_ahead)).timestamp())
        if not rows:
            return "No posts in that window."
        lines = []
        for r in rows:
            when = datetime.fromtimestamp(r["run_at"], timezone.utc).isoformat(timespec="minutes")
            line = f"- [{r['status']}] {when} {r['platform']}: {r['title']} (post `{r['id']}`)"
            if r["result_url"]:
                line += f" → {r['result_url']}"
            if r["error"]:
                line += f"\n  {r['error']}"
            lines.append(line)
        return "Posts:\n" + "\n".join(lines)

    @mcp.tool(title="Cancel a scheduled post", annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def inkcast_cancel(post_id: Annotated[str, Field(description="Post id from inkcast_posts or inkcast_publish.")]) -> str:
        """Cancel a scheduled post before it goes out."""
        try:
            title = publisher.cancel(_user_id(), post_id)
        except PlatformError as exc:
            raise ToolError(str(exc)) from exc
        return f"Cancelled “{title}” (post `{post_id}`)."

    # ---- browser routes: sign-in, consent, connect ----------------------

    @mcp.custom_route("/auth/login", methods=["GET"])
    async def auth_login(request: Request) -> Response:
        pending_id = request.query_params.get("pending", "")
        if not store.get_pending(pending_id):
            return pages.message("Link expired", "This sign-in link expired. Start again from your MCP client.", error=True, status=400)
        try:
            return await identity.start(request, pending_id)
        except LoginError as exc:
            return pages.message("Can't sign in", str(exc), error=True, status=502)

    @mcp.custom_route("/auth/callback", methods=["GET", "POST"])
    async def auth_callback(request: Request) -> Response:
        try:
            pending_id, who = await identity.finish(request)
        except LoginError as exc:
            if config.login == "password" and request.method == "POST":
                form = await request.form()
                return pages.password_form(str(form.get("pending", "")), str(exc))
            return pages.message("Sign-in failed", str(exc), error=True, status=403)
        if not store.get_pending(pending_id):
            return pages.message("Link expired", "This sign-in took too long. Start again from your MCP client.", error=True, status=400)
        user = store.upsert_user(who.key, who.display, who.email, who.name)
        store.set_pending_user(pending_id, user["id"])
        return RedirectResponse(f"/auth/consent?pending={pending_id}", status_code=303)

    @mcp.custom_route("/auth/consent", methods=["GET", "POST"])
    async def auth_consent(request: Request) -> Response:
        data = request.query_params if request.method == "GET" else await request.form()
        pending = store.get_pending(str(data.get("pending", "")))
        if not pending or not pending["user_id"]:
            return pages.message("Link expired", "This request expired. Start again from your MCP client.", error=True, status=400)
        if request.method == "GET":
            client_name, host = oauth.describe_pending(pending)
            user = store.get_user(pending["user_id"]) or {}
            return pages.consent(pending["id"], pending["csrf"], client_name, host, user.get("display", "?"))
        if data.get("csrf") != pending["csrf"]:
            return pages.message("Request rejected", "The form was invalid. Start again from your MCP client.", error=True, status=400)
        target = oauth.approve(pending) if data.get("decision") == "approve" else oauth.deny(pending)
        return RedirectResponse(target, status_code=303)

    @mcp.custom_route("/connect/{ticket}", methods=["GET", "POST"])
    async def connect(request: Request) -> Response:
        ticket = request.path_params["ticket"]
        row = store.get_ticket(ticket)
        if not row or row["platform"] not in registry:
            return pages.message("Link expired", "This connect link expired or was used. Ask Claude for a new one.", error=True, status=400)
        p = registry[row["platform"]]
        if request.method == "GET":
            return pages.connect_form(ticket, p.name, p.connect_help, p.credential_fields)
        form = await request.form()
        creds = {f.key: str(form.get(f.key, "")).strip() for f in p.credential_fields}
        try:
            account = await p.verify(creds)
        except PlatformError as exc:
            return pages.connect_form(ticket, p.name, p.connect_help, p.credential_fields, error=str(exc))
        store.save_credentials(row["user_id"], p.id, creds, account.to_dict())
        store.consume_ticket(ticket)
        who = account.handle or account.name
        return pages.message(f"{p.name} connected", f"Connected as {who}. You can close this tab and go back to Claude.")

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__, "platforms": list(registry)})

    return app


def asgi_app(app: App):
    c = app.config
    return app.mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=c.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=c.allowed_hosts, allowed_origins=[],
        ),
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    config.validate()
    app = build(config)
    log.info("Inkcast %s — MCP endpoint %s (sign-in: %s)", __version__, config.mcp_url, config.login)
    log.info("Platforms: %s", ", ".join(app.platforms))
    log.info("Add to Claude Code: claude mcp add --transport http inkcast %s", config.mcp_url)

    server = uvicorn.Server(uvicorn.Config(asgi_app(app), host=config.host, port=config.port,
                                           proxy_headers=True, forwarded_allow_ips="*"))

    async def run() -> None:
        scheduler = asyncio.create_task(
            app.publisher.run_forever(config.scheduler_interval_seconds, housekeeping=app.oauth.housekeeping)
        )
        try:
            await server.serve()
        finally:
            scheduler.cancel()

    asyncio.run(run())


if __name__ == "__main__":
    main()
