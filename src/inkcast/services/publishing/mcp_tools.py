# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The MCP endpoint (/mcp): thin text renderings of the same use cases as /api/v1.

The endpoint is a resource server only: tokens come from IdentityService (OAuth) or are personal
access tokens, verified by libs.auth.TokenVerifier. Each tool checks the caller's scope.
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from urllib.parse import urlencode

from pydantic import Field

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from ... import __version__
from ...libs.account import deletion_link
from ...libs.auth import SCOPE, SCOPES, Principal
from ...libs.config import PAT_DAYS_CEILING
from ...libs.content.story import SCHEDULE_HORIZON_DAYS
from ...libs.contracts.models import PublishRequest, PublishResponse, SyncFile, SyncRequest, SyncResponse
from ...libs.runtime import Context
from .sync import GitSync
from .usecases import InkcastService, ServiceError

INSTRUCTIONS = """\
Inkcast publishes and schedules stories to writing platforms (see inkcast_platforms).

To publish a markdown file: read the file yourself and pass its FULL text as `markdown`
(this server may run remotely and cannot read local paths). YAML front matter (title,
subtitle, tags, canonical_url, cover_image, platforms, publish_at, and per-platform blocks
like `medium:` or `devto:`) is used automatically; explicit arguments override it.

Before the first real publish of a file, call inkcast_publish with dry_run=true and show the
person the per-platform preview and warnings. If a platform isn't connected, call
inkcast_connect and give the person the link; never ask them to paste API keys into chat.

For files that live in a git repository, prefer inkcast_sync: it remembers what each file
became on each platform, so running it again updates posts instead of duplicating them.
For CI, the person needs a personal access token: call inkcast_create_token and give them
the link (the token is shown to them in the browser, never to you).

If the person wants to delete their Inkcast account, call inkcast_delete_account and give
them the link: they sign in again, read what goes and what stays, and confirm at the sign-in
provider. You never delete anything yourself.
"""


def render_publish(resp: PublishResponse, registry: dict) -> str:
    head = [("DRY RUN — nothing was sent." if resp.dry_run else "")]
    head.append(f"“{resp.title}”" + (f" — {resp.subtitle}" if resp.subtitle else ""))
    head += [f"warning: {w}" for w in resp.warnings]
    lines = [h for h in head if h]
    for r in resp.results:
        name = registry[r.platform].name if r.platform in registry else r.platform
        if r.status == "preview":
            block = [f"[{name}]" + ("" if r.connected else " — NOT CONNECTED")]
            block += [f"  {k}: {v}" for k, v in (r.preview or {}).items()]
            if r.message:
                block.append(f"  {r.message}")
        elif r.status == "scheduled":
            block = [f"- {r.platform}: scheduled for {resp.publish_at} (post `{r.post_id}`)"]
        elif r.status in ("published", "draft", "unlisted"):
            block = [f"- {r.platform}: {r.status}" + (f" → {r.url}" if r.url else "") + f" (post `{r.post_id}`)"]
        elif r.status == "unconfirmed":
            block = [f"- {r.platform}: UNCONFIRMED — {r.message} (post `{r.post_id}`)"]
        elif r.status == "skipped":
            block = [f"- {r.platform}: skipped — {r.message}"]
        else:
            block = [f"- {r.platform}: FAILED — {r.message}"]
        block += [f"  note: {n}" for n in r.notes]
        block += [f"  warning: {w}" for w in r.warnings]
        lines.append("\n".join(block))
    return "\n".join(lines)


def render_sync(resp: SyncResponse) -> str:
    lines = [("DRY RUN — nothing was sent. " if resp.dry_run else "") + f"Sync of {resp.source}"
             + (f" @ {resp.ref}" if resp.ref else "") + ":"]
    for r in resp.results:
        line = f"- {r.path} → {r.platform}: {r.action.upper()}" + (f" ({r.status})" if r.status else "")
        if r.url:
            line += f" → {r.url}"
        if r.message:
            line += f"\n  {r.message}"
        if r.preview and resp.dry_run:
            line += "\n" + "\n".join(f"  {k}: {v}" for k, v in r.preview.items())
        lines.append(line)
    return "\n".join(lines)


def build_mcp(ctx: Context, service: InkcastService, sync: GitSync) -> MCPServer:
    config, registry = ctx.config, ctx.platforms
    mcp = MCPServer(
        name="inkcast",
        title="Inkcast",
        instructions=INSTRUCTIONS,
        version=__version__,
        token_verifier=ctx.verifier,
        auth=AuthSettings(
            issuer_url=config.public_url,
            resource_server_url=config.mcp_url,
            required_scopes=[SCOPE],
            validate_token_resource=False,
        ),
    )

    def principal(scope: str) -> Principal:
        access = get_access_token()
        who = ctx.verifier.principal(access.token) if access else None
        if who is None:
            raise ToolError("Not signed in. Run /mcp in Claude Code and authenticate Inkcast.")
        if not who.can(scope):
            raise ToolError(f"This token lacks the `{scope}` scope.")
        return who

    async def call(scope: str, fn, *args):
        """Run a use case for the caller if their token holds ``scope``; service errors become tool errors."""
        who = principal(scope)
        try:
            result = fn(who.user_id, *args)
            return await result if asyncio.iscoroutine(result) else result
        except ServiceError as exc:
            raise ToolError(exc.message) from exc

    @mcp.tool(title="List platforms", annotations=ToolAnnotations(read_only_hint=True))
    async def inkcast_platforms() -> str:
        """List the publishing platforms Inkcast supports and which ones you have connected."""
        lines = []
        for p in await call("connections:read", service.list_platforms):
            state = f"connected as {p.account.handle or p.account.name}" if p.account else "not connected"
            dest = f"; supports {p.destination_label}s" if p.destination_label else ""
            lines.append(f"- `{p.id}` — {p.name}: {state}{dest}")
        return "Platforms:\n" + "\n".join(lines)

    @mcp.tool(title="Connect a platform", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    async def inkcast_connect(platform_id: Annotated[str, Field(description="Platform id from inkcast_platforms, e.g. 'devto'.")]) -> str:
        """Get a one-time link where the person enters this platform's credentials (never paste keys into chat)."""
        link = await call("connections:write", service.connect_link, platform_id)
        name = registry[link.platform].name
        return (f"Open this link to connect {name} (valid {link.expires_in // 60} minutes, one use):\n{link.url}\n\n"
                f"{link.help}")

    @mcp.tool(title="Disconnect a platform", annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def inkcast_disconnect(platform_id: Annotated[str, Field(description="Platform id to disconnect.")]) -> str:
        """Delete the stored credentials for a platform. Scheduled posts to it will fail until it is reconnected."""
        removed = await call("connections:write", service.disconnect, platform_id)
        name = registry[platform_id.strip().lower()].name
        return f"Disconnected {name}." if removed else f"{name} wasn't connected."

    @mcp.tool(title="List destinations", annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def inkcast_destinations(platform_id: Annotated[str, Field(description="Platform id, e.g. 'medium'.")]) -> str:
        """List where you can post on a platform besides your own profile (Medium publications, DEV organizations...)."""
        p = registry.get(platform_id.strip().lower())
        dests = await call("connections:read", service.destinations, platform_id)
        if p and not p.destination_label:
            return f"{p.name} has no destinations besides your own profile."
        if not dests:
            label = p.destination_label if p else "destination"
            return (f"{p.name if p else platform_id} can't list {label}s; pass the {label}'s name or id as the "
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
        publish_at: Annotated[str | None, Field(description=f"Schedule time, ISO 8601 WITH a UTC offset, e.g. 2026-10-21T09:00:00-05:00, at most {SCHEDULE_HORIZON_DAYS} days ahead. Omit to publish now.")] = None,
        draft: Annotated[bool, Field(description="Create drafts on the platforms instead of publishing.")] = False,
        image_base_url: Annotated[str | None, Field(description="Base URL to turn relative image paths into absolute ones.")] = None,
        dry_run: Annotated[bool, Field(description="Validate and preview each platform's version; nothing is sent or saved.")] = False,
    ) -> str:
        """Publish, schedule or draft one markdown story to one or more platforms. Use dry_run=true first to preview."""
        req = PublishRequest(markdown=markdown, platforms=platforms, title=title, subtitle=subtitle, tags=tags,
                             canonical_url=canonical_url, cover_image=cover_image, destinations=destinations,
                             publish_at=publish_at, draft=draft, image_base_url=image_base_url, dry_run=dry_run)
        return render_publish(await call("posts:read" if dry_run else "posts:write", service.publish, req), registry)

    @mcp.tool(
        title="Sync files from a repository",
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=True),
    )
    async def inkcast_sync(
        source: Annotated[str, Field(description="Stable repository id, e.g. 'github.com/acme/blog'.")],
        files: Annotated[dict[str, str], Field(description="Map of repository path → FULL markdown text, e.g. {'posts/hello.md': '---\\ntitle: …'}.")],
        platforms: Annotated[list[str] | None, Field(description="Override each file's front matter `platforms`.")] = None,
        image_base_url: Annotated[str | None, Field(description="Base URL that relative image paths resolve against.")] = None,
        dry_run: Annotated[bool, Field(description="Report what would happen; change nothing.")] = False,
    ) -> str:
        """Publish new files, update changed ones in place where the platform allows, skip unchanged ones. Never duplicates."""
        req = SyncRequest(source=source, files=[SyncFile(path=p, markdown=m) for p, m in files.items()],
                          platforms=platforms, image_base_url=image_base_url, dry_run=dry_run)
        return render_sync(await call("posts:read" if dry_run else "stories:sync", sync.sync, req))

    @mcp.tool(title="List posts", annotations=ToolAnnotations(read_only_hint=True))
    async def inkcast_posts(
        days_back: Annotated[int, Field(ge=0, le=366, description="Include posts from this many days ago.")] = 7,
        days_ahead: Annotated[int, Field(ge=0, le=SCHEDULE_HORIZON_DAYS, description="Include posts scheduled up to this many days ahead (the maximum covers every schedule).")] = 90,
    ) -> str:
        """List your scheduled, published, draft, failed and cancelled posts."""
        posts = await call("posts:read", service.list_posts, days_back, days_ahead)
        if not posts:
            return "No posts in that window."
        lines = []
        for p in posts:
            line = f"- [{p.status}] {p.run_at[:16]} {p.platform}: {p.title} (post `{p.id}`)"
            if p.url:
                line += f" → {p.url}"
            if p.error:
                line += f"\n  {p.error}"
            lines.append(line)
        return "Posts:\n" + "\n".join(lines)

    @mcp.tool(title="Cancel a scheduled post", annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def inkcast_cancel(post_id: Annotated[str, Field(description="Post id from inkcast_posts or inkcast_publish.")]) -> str:
        """Cancel a scheduled post before it goes out."""
        post = await call("posts:write", service.cancel, post_id)
        return f"Cancelled “{post.title}” (post `{post.id}`)."

    # This server's policy for personal access tokens (F1.10.S2, issue #90): the tool states it (its description and
    # the parameter's default; the annotations themselves must be module-level names, the SDK evaluates them without
    # this closure) and enforces it, so the link handed out is never one the page would refuse. The schema's ceiling
    # stays 366, like the API document's; the configured maximum may be lower.
    pat = config.pat
    never = ("null = never, which the person confirms on the page." if pat.allow_no_expiry
             else "Every token expires on this server: null (never) is refused.")

    @mcp.tool(title="Create a personal access token", annotations=ToolAnnotations(read_only_hint=True), description=(
        "Give the person a link where they sign in and get a token for CI (e.g. the GitHub Action). You never see the "
        f"token. On this server a token lasts {pat.default_days} days unless expires_in_days says otherwise, at most "
        f"{pat.max_days}. {never}"))
    async def inkcast_create_token(
        name: Annotated[str, Field(min_length=1, max_length=80, description="What it's for, e.g. 'blog repo CI'.")],
        scopes: Annotated[list[str], Field(description=f"Scopes; one or more of {', '.join(SCOPES)}. CI sync needs 'stories:sync'.")] = ["stories:sync"],  # noqa: B006
        expires_in_days: Annotated[int | None, Field(ge=1, le=PAT_DAYS_CEILING, description=(
            "Days until it stops working; omit for this server's default. The server's maximum may be lower than 366 "
            "(the tool description states it; a refusal names it). null = never, where the server allows it."))] = pat.default_days,
    ) -> str:
        """The link to /tokens/new (the description above, with this server's lifetimes, is what the client sees)."""
        who = principal("posts:read")
        if who.kind != "oauth":
            raise ToolError("A personal access token can't create tokens; sign in with OAuth (/mcp in Claude Code).")
        unknown = [s for s in scopes if s not in SCOPES]
        if unknown:
            raise ToolError(f"Unknown scope(s): {', '.join(unknown)}. Choose from: {', '.join(SCOPES)}.")
        why = pat.refusal(expires_in_days)
        if why:
            raise ToolError(why)
        lifetime = "never expires" if expires_in_days is None else f"{expires_in_days} days"
        query = urlencode({"name": name, "scopes": " ".join(scopes),
                           "days": "never" if expires_in_days is None else expires_in_days})
        return (f"Open this link to create the token “{name}” ({', '.join(scopes)}; {lifetime}). "
                f"You'll sign in, confirm, and see the token once — store it as a CI secret named INKCAST_TOKEN:\n"
                f"{config.public_url}/tokens/new?{query}")

    @mcp.tool(title="Delete my account", annotations=ToolAnnotations(read_only_hint=True))
    async def inkcast_delete_account() -> str:
        """Give the person a one-time link to delete their Inkcast account. Nothing is deleted by this call: on the
        page they sign in again, read what goes (posts, schedules, the ledger, connections and credentials, tokens,
        usage numbers) and what stays (articles already on the platforms), and confirm at the sign-in provider."""
        who = principal("posts:read")
        if who.kind != "oauth":
            raise ToolError("A personal access token can't start an account deletion; sign in with OAuth (/mcp in Claude Code).")
        try:
            link = deletion_link(config, ctx.store, who.user_id)
        except ServiceError as exc:
            raise ToolError(exc.message) from exc
        return (f"Open this link to delete your Inkcast account (valid {link.expires_in // 60} minutes, one use):\n{link.url}\n\n"
                "You'll sign in again, see what goes and what stays, then confirm at the sign-in provider. Articles already "
                "published on the platforms stay there. Nothing has been deleted yet.")

    return mcp


def mcp_app(ctx: Context, mcp: MCPServer):
    c = ctx.config
    return mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=c.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=c.allowed_hosts, allowed_origins=[],
        ),
    )
