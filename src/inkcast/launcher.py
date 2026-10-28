# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Run one, several or all Inkcast services from the one image.

    inkcast                          # INKCAST_APP_NAME, default All
    inkcast run PublishingApi        # one service
    inkcast run IdentityService,ConnectorService
    inkcast services                 # list what can run
    inkcast checks                   # the security posture table for this configuration
    inkcast identity ...             # resolve a pass the Keycloak user sync's breaker holds (breaker.py)
    inkcast authz write-model        # OpenFGA: the store and the model version to pin (authz_cli.py)
    inkcast all | web | worker       # v0.1 modes, still accepted

Configuration problems stop the process before it serves anything, with exit code 78
(EX_CONFIG) and a list of what to fix, so a bad rollout fails fast and visibly. That gate covers
reading the configuration (a dangling NAME_FILE, a bad number), ``Config.validate`` for the services
in this process, and the security posture checks (libs/secrets/checks.py): a ``fail`` stops the
process, a ``warn`` is logged once with the event ``config.posture.warn``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from importlib.metadata import entry_points

from . import __version__
from .libs.config import Config, ConfigError, load_config
from .libs.http.app import compose, metrics_app
from .libs.observability import setup_logging
from .libs.runtime import Context, build_context
from .libs.secrets import problems, run_checks
from .libs.secrets.checks import render
from .libs.service import ServiceSpec, is_camel
from .services import connectors, gateway, identity, insights, publishing, scheduler

log = logging.getLogger("inkcast")

EX_CONFIG = 78
BUILTIN: dict[str, ServiceSpec] = {s.name: s for s in (
    identity.SERVICE, publishing.SERVICE, connectors.SERVICE, scheduler.SERVICE, insights.SERVICE, gateway.SERVICE)}
# "All" is every built-in service that serves the product itself (not the Gateway: in a single
# process there is nothing to route to).
ALL = ("IdentityService", "PublishingApi", "ConnectorService", "SchedulerWorker", "InsightsService")
LEGACY_MODES = {"all": "All", "web": "IdentityService,PublishingApi,ConnectorService,InsightsService",
                "worker": "SchedulerWorker"}


def registry() -> dict[str, ServiceSpec]:
    found = dict(BUILTIN)
    for ep in entry_points(group="inkcast.services"):
        try:
            spec = ep.load()
        except Exception:  # a broken plug-in must not take the process down
            log.exception("Could not load service plug-in %r", ep.name)
            continue
        if isinstance(spec, ServiceSpec) and spec.name not in BUILTIN:
            found[spec.name] = spec
        else:
            log.error("Entry point %r is not a new ServiceSpec; skipped", ep.name)
    return found


def resolve(app_name: str) -> list[ServiceSpec]:
    """``"All"``, ``"PublishingApi"`` or ``"IdentityService,ConnectorService"`` → specs, in a stable order."""
    known = registry()
    names: list[str] = []
    for raw in app_name.split(","):
        name = LEGACY_MODES.get(raw.strip().lower(), raw.strip())
        for part in name.split(","):
            part = part.strip()
            if not part:
                continue
            if part == "All":
                names += [n for n in ALL if n not in names]
            elif part in known:
                if part not in names:
                    names.append(part)
            else:
                hint = " (names are CamelCase)" if not is_camel(part) else ""
                raise ConfigError(f"Unknown service {part!r}{hint}. Available: All, {', '.join(sorted(known))}.")
    if not names:
        raise ConfigError("INKCAST_APP_NAME is empty.")
    return [known[n] for n in names]


def build(config: Config, specs: list[ServiceSpec], transport=None) -> Context:
    storage = any(s.name != "Gateway" for s in specs)
    return build_context(config, transport, services=tuple(s.name for s in specs), storage=storage)


class _Server:
    """uvicorn without its own signal handling: Inkcast coordinates shutdown of all its parts."""

    def __init__(self, asgi, host: str, port: int, grace: float, forwarded: str):
        import uvicorn

        class Quiet(uvicorn.Server):
            def capture_signals(self):  # noqa: D401 - signals are handled by main()
                return contextlib.nullcontext()

        # The grace as given: uvicorn waits asyncio.wait_for(..., timeout=grace), and int() would make 0.5 a 0 (no grace).
        self.server = Quiet(uvicorn.Config(asgi, host=host, port=port, proxy_headers=True,
                                           forwarded_allow_ips=forwarded, timeout_graceful_shutdown=grace,
                                           log_config=None))

    async def serve(self) -> None:
        await self.server.serve()

    def stop(self) -> None:
        self.server.should_exit = True


def _list_services() -> None:
    for spec in registry().values():
        print(f"{spec.name:<18} {spec.full_name:<20} {spec.kind:<7} {spec.summary}")
    print(f"{'All':<18} {'inkcast':<20} {'both':<7} {', '.join(ALL)} in one process")


def refuse(message: str) -> None:
    """Stop before serving anything: EX_CONFIG, the reason on stderr and as the ``config.unsafe`` log event."""
    log.error("Refusing to start: %s", message, extra={"event": "config.unsafe", "fatal": True})
    print(f"inkcast: refusing to start.\n{message}", file=sys.stderr)
    sys.exit(EX_CONFIG)


def gate(config: Config, specs: list[ServiceSpec]) -> None:
    """``Config.validate`` for these services, then the posture checks: ``fail`` refuses, ``warn`` is logged."""
    needs = {n for s in specs for n in s.needs}
    config.validate(needs)
    fails, warns = problems(run_checks(config))
    if fails:
        raise ConfigError("Security posture problems:\n- " + "\n- ".join(f"{c.title}: {c.detail}" for c in fails))
    for c in warns:
        log.warning("Posture: %s: %s", c.title, c.detail, extra={"event": "config.posture.warn", "check": c.id})
    if config.secrets:
        config.secrets.startup_done()
        for s in config.secrets.status():
            log.info("Secret %s: %s (%s, fp=%s)", s["name"], "set" if s["present"] else "not set", s["source"],
                     s["fingerprint"], extra={"event": "secrets.status", "secret_name": s["name"], "source": s["source"]})


def main(argv: list[str] | None = None) -> None:
    import argparse

    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["sync"]:  # the CI/laptop client has its own options
        from .cli import sync_main
        sys.exit(sync_main(argv[1:]))
    if argv[:1] in (["backup"], ["restore"], ["backups"]):  # operations on the database file, with their own options
        from .backup import backup_main, list_main, restore_main
        sys.exit({"backup": backup_main, "restore": restore_main, "backups": list_main}[argv[0]](argv[1:]))
    if argv[:1] == ["identity"]:  # an operator resolves a pass the Keycloak user sync's breaker holds (#105)
        from .breaker import identity_main
        sys.exit(identity_main(argv[1:]))
    if argv[:1] == ["authz"]:  # an operator writes the model to OpenFGA and pins the ids it prints (#279)
        from .authz_cli import authz_main
        sys.exit(authz_main(argv[1:]))

    parser = argparse.ArgumentParser(prog="inkcast", description=f"Inkcast {__version__}")
    parser.add_argument("command", nargs="?", default=None,
                        help="run (default) | services | checks | sync | backup | restore | backups | identity | authz, "
                             "or a v0.1 mode: all | web | worker")
    parser.add_argument("names", nargs="?", default=None, help="for run: CamelCase service name(s), comma-separated")
    args = parser.parse_args(argv)

    if args.command == "services":
        _list_services()
        return

    if args.command not in (None, "run", "checks", *LEGACY_MODES):
        parser.error(f"unknown command {args.command!r}")
    # Logging first, so even a failure to read the configuration is a well-formed log line.
    setup_logging(os.environ.get("INKCAST_LOG_FORMAT", "text").lower(), os.environ.get("INKCAST_LOG_LEVEL", "INFO"),
                  service=os.environ.get("INKCAST_APP_NAME", "All"))
    try:
        config = load_config()
    except ConfigError as exc:
        refuse(str(exc))
        return  # pragma: no cover - refuse() exits
    if args.command == "checks":
        render(run_checks(config))
        return
    if args.command == "run" and args.names:
        app_name = args.names
    elif args.command in LEGACY_MODES:
        app_name = LEGACY_MODES[args.command]
    else:
        app_name = config.app_name
    service_label = app_name if "," not in app_name else "multi"
    setup_logging(config.log_format, config.log_level, service=service_label)
    try:
        specs = resolve(app_name)
        gate(config, specs)
    except ConfigError as exc:
        refuse(str(exc))
        return  # pragma: no cover - refuse() exits

    ctx = build(config, specs)
    http_specs = [s for s in specs if s.kind == "http"]
    workers = [(s, w) for s in specs for w in s.workers]
    forwarded = os.environ.get("INKCAST_FORWARDED_ALLOW_IPS", "127.0.0.1")
    log.info("Inkcast %s starting %s on %s (prefix %s); public URL %s; platforms: %s",
             __version__, ", ".join(s.name for s in specs), f"{config.host}:{config.port}" if http_specs else "-",
             config.path_prefix, config.public_url, ", ".join(ctx.platforms))

    servers: list[_Server] = []
    if http_specs:
        servers.append(_Server(compose(http_specs, ctx), config.host, config.port, config.shutdown_grace_seconds, forwarded))
    if config.metrics_port:
        servers.append(_Server(metrics_app(specs, ctx), config.host, config.metrics_port, 5, "127.0.0.1"))

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
        tasks += [asyncio.create_task(worker(ctx, stop)) for _, worker in workers]
        await asyncio.gather(*tasks)
        await ctx.http.aclose()
        client = ctx.extras.get("gateway_client")
        if client:
            await client.aclose()
        log.info("Inkcast stopped")

    asyncio.run(run())


if __name__ == "__main__":
    main()
