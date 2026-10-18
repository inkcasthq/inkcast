"""Inkcast: publish and schedule writing across platforms from Claude Code (or any MCP client)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Annotated

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
from .api import build_routes
from .models import PublishRequest, PublishResponse
from .observability import Metrics, setup_logging
from .platforms import Platform, PlatformError, load_platforms
from .publisher import Publisher
from .service import InkcastService, ServiceError
from .storage import Store

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
    metrics: Metrics
    runs_scheduler: bool = False
    service: "InkcastService | None" = None


def _user_id() -> str:
    access = get_access_token()
    if access is None or not access.subject:
        raise ToolError("Not signed in. Run /mcp in Claude Code and authenticate Inkcast.")
    return access.subject


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
        elif r.status == "unknown":
            block = [f"- {r.platform}: UNKNOWN — {r.message} (post `{r.post_id}`)"]
        elif r.status == "skipped":
            block = [f"- {r.platform}: skipped — {r.message}"]
        else:
            block = [f"- {r.platform}: FAILED — {r.message}"]
        block += [f"  note: {n}" for n in r.notes]
        block += [f"  warning: {w}" for w in r.warnings]
        lines.append("\n".join(block))
    return "\n".join(lines)


def _metrics(store: Store, registry: dict) -> Metrics:
    m = Metrics()
    m.gauge("inkcast_build_info", "Inkcast version and installed platform plug-ins.",
            lambda: {(("platforms", ",".join(registry)), ("version", __version__)): 1})
    m.counter("inkcast_publish_total", "Publish attempts by platform and outcome.")
    m.counter("inkcast_scheduler_ticks_total", "Completed scheduler ticks.")
    m.gauge("inkcast_posts", "Posts by status.", lambda: {(("status", k),): v for k, v in store.post_counts().items()})
    m.gauge("inkcast_posts_due", "Scheduled posts that are due now (queue depth).",
            lambda: {(): store.due_count(time.time())})
    return m


def build(config: Config, transport: httpx2.AsyncBaseTransport | None = None) -> App:
    store = Store(config.database_path, config.secret_key)
    http = httpx2.AsyncClient(timeout=httpx2.Timeout(30.0, connect=10.0), transport=transport, follow_redirects=False)
    registry = load_platforms(http, config.platform_urls)
    metrics = _metrics(store, registry)
    publisher = Publisher(store, registry, metrics)
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
    app = App(config, store, http, registry, publisher, oauth, identity, mcp, metrics)

    service = InkcastService(store, registry, publisher, config.public_url)
    app.service = service

    async def call(fn, *args):
        """Run a service call for the signed-in user; turn service errors into tool errors."""
        try:
            result = fn(_user_id(), *args)
            return await result if asyncio.iscoroutine(result) else result
        except ServiceError as exc:
            raise ToolError(exc.message) from exc

    # ---- MCP tools: thin text renderings of the service (same use cases as /api/v1) ----

    @mcp.tool(title="List platforms", annotations=ToolAnnotations(read_only_hint=True))
    async def inkcast_platforms() -> str:
        """List the publishing platforms Inkcast supports and which ones you have connected."""
        lines = []
        for p in await call(service.list_platforms):
            state = f"connected as {p.account.handle or p.account.name}" if p.account else "not connected"
            dest = f"; supports {p.destination_label}s" if p.destination_label else ""
            lines.append(f"- `{p.id}` — {p.name}: {state}{dest}")
        return "Platforms:\n" + "\n".join(lines)

    @mcp.tool(title="Connect a platform", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    async def inkcast_connect(platform_id: Annotated[str, Field(description="Platform id from inkcast_platforms, e.g. 'devto'.")]) -> str:
        """Get a one-time link where the person enters this platform's credentials (never paste keys into chat)."""
        link = await call(service.connect_link, platform_id)
        name = registry[link.platform].name
        return (f"Open this link to connect {name} (valid {link.expires_in // 60} minutes, one use):\n{link.url}\n\n"
                f"{link.help}")

    @mcp.tool(title="Disconnect a platform", annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def inkcast_disconnect(platform_id: Annotated[str, Field(description="Platform id to disconnect.")]) -> str:
        """Delete the stored credentials for a platform. Scheduled posts to it will fail until it is reconnected."""
        removed = await call(service.disconnect, platform_id)
        name = registry[platform_id.strip().lower()].name
        return f"Disconnected {name}." if removed else f"{name} wasn't connected."

    @mcp.tool(title="List destinations", annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def inkcast_destinations(platform_id: Annotated[str, Field(description="Platform id, e.g. 'medium'.")]) -> str:
        """List where you can post on a platform besides your own profile (Medium publications, DEV organizations...)."""
        p = registry.get(platform_id.strip().lower())
        dests = await call(service.destinations, platform_id)
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
        publish_at: Annotated[str | None, Field(description="Schedule time, ISO 8601 WITH a UTC offset, e.g. 2026-10-21T09:00:00-05:00. Omit to publish now.")] = None,
        draft: Annotated[bool, Field(description="Create drafts on the platforms instead of publishing.")] = False,
        image_base_url: Annotated[str | None, Field(description="Base URL to turn relative image paths into absolute ones.")] = None,
        dry_run: Annotated[bool, Field(description="Validate and preview each platform's version; nothing is sent or saved.")] = False,
    ) -> str:
        """Publish, schedule or draft one markdown story to one or more platforms. Use dry_run=true first to preview."""
        req = PublishRequest(markdown=markdown, platforms=platforms, title=title, subtitle=subtitle, tags=tags,
                             canonical_url=canonical_url, cover_image=cover_image, destinations=destinations,
                             publish_at=publish_at, draft=draft, image_base_url=image_base_url, dry_run=dry_run)
        return render_publish(await call(service.publish, req), registry)

    @mcp.tool(title="List posts", annotations=ToolAnnotations(read_only_hint=True))
    async def inkcast_posts(
        days_back: Annotated[int, Field(ge=0, le=366, description="Include posts from this many days ago.")] = 7,
        days_ahead: Annotated[int, Field(ge=0, le=730, description="Include posts scheduled up to this many days ahead.")] = 90,
    ) -> str:
        """List your scheduled, published, draft, failed and cancelled posts."""
        posts = await call(service.list_posts, days_back, days_ahead)
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
        post = await call(service.cancel, post_id)
        return f"Cancelled “{post.title}” (post `{post.id}`)."

    # ---- REST API, OpenAPI, Swagger UI and ReDoc ---------------------------
    for route in build_routes(service, oauth.load_access_token, config.public_url):
        mcp.custom_route(route.path, methods=sorted(route.methods - {"HEAD"}))(route.endpoint)

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

    for path, handler in health_routes(app).items():
        mcp.custom_route(path, methods=["GET"])(handler)

    return app


def health_routes(app: "App") -> dict:
    """Kubernetes-style probes. /livez: the process serves requests. /readyz: storage answers and,
    when this process runs the scheduler, its loop ticked recently."""

    async def livez(_: Request) -> Response:
        return JSONResponse({"ok": True})

    async def readyz(_: Request) -> Response:
        problems = []
        try:
            app.store.ping()
        except Exception as exc:  # pragma: no cover - depends on storage failure
            problems.append(f"storage: {exc}")
        if app.runs_scheduler and app.publisher.last_tick and \
                time.time() - app.publisher.last_tick > max(120, 6 * app.config.scheduler_interval_seconds):
            problems.append("scheduler: no tick recently")
        body = {"ok": not problems, "version": __version__, "platforms": list(app.platforms)}
        if problems:
            body["problems"] = problems
        return JSONResponse(body, status_code=200 if not problems else 503)

    return {"/livez": livez, "/readyz": readyz, "/healthz": readyz}


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


def metrics_app(app: App):
    """Prometheus /metrics plus probes, on a separate port that is never exposed publicly."""
    from starlette.routing import Route

    m = app.metrics
    starlette_app = m.asgi()
    for path, handler in health_routes(app).items():
        starlette_app.router.routes.append(Route(path, handler))
    return starlette_app


class _Server:
    """uvicorn without its own signal handling: Inkcast coordinates shutdown of all its parts."""

    def __init__(self, asgi, host: str, port: int, grace: float, forwarded: str):
        import uvicorn

        class Quiet(uvicorn.Server):
            def capture_signals(self):  # noqa: D401 - signals are handled by main()
                import contextlib
                return contextlib.nullcontext()

        self.server = Quiet(uvicorn.Config(asgi, host=host, port=port, proxy_headers=True,
                                           forwarded_allow_ips=forwarded, timeout_graceful_shutdown=int(grace),
                                           log_config=None))

    async def serve(self) -> None:
        await self.server.serve()

    def stop(self) -> None:
        self.server.should_exit = True


def main(argv: list[str] | None = None) -> None:
    import argparse
    import os
    import signal

    parser = argparse.ArgumentParser(prog="inkcast", description="Inkcast MCP server")
    parser.add_argument("mode", nargs="?", default="all", choices=["all", "web", "worker"],
                        help="all = web + scheduler (single node); web = HTTP only; worker = scheduler only")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.log_format, config.log_level)
    config.validate()
    app = build(config)
    app.runs_scheduler = args.mode in ("all", "worker")
    forwarded = os.environ.get("INKCAST_FORWARDED_ALLOW_IPS", "127.0.0.1")
    log.info("Inkcast %s starting in %s mode; MCP endpoint %s; sign-in: %s; platforms: %s",
             __version__, args.mode, config.mcp_url, config.login, ", ".join(app.platforms))

    servers: list[_Server] = []
    if args.mode in ("all", "web"):
        servers.append(_Server(asgi_app(app), config.host, config.port, config.shutdown_grace_seconds, forwarded))
    if config.metrics_port:
        servers.append(_Server(metrics_app(app), config.host, config.metrics_port, 5, "127.0.0.1"))

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_signal(signame: str) -> None:
            log.info("%s received: finishing in-flight work, then exiting", signame)
            stop.set()
            for srv in servers:
                srv.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, on_signal, sig.name)

        tasks = [asyncio.create_task(srv.serve()) for srv in servers]
        if app.runs_scheduler:
            tasks.append(asyncio.create_task(app.publisher.run_forever(
                config.scheduler_interval_seconds, stop, housekeeping=app.oauth.housekeeping)))
        await asyncio.gather(*tasks)
        await app.http.aclose()
        log.info("Inkcast stopped")

    asyncio.run(run())


if __name__ == "__main__":
    main()
