# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""`inkcast identity`: an operator resolves a pass the Keycloak user sync's circuit breaker holds (E9.F9.5.S7, #105).

    inkcast identity trips                                    # every pass that tripped, oldest first, with its status
    inkcast identity confirm-deletions --pass ID [--by WHO]   # the people really are gone: cut them off, start the clock
    inkcast identity dismiss-deletions --pass ID [--by WHO]   # Keycloak was wrong (and is fixed): do nothing, read again

A reconcile that found too many people gone from Keycloak at once recorded the pass and held them: nobody was cut off
or erased and no grace period started (services/identity/keycloak_sync.py). Confirming applies exactly that pass's
people, as the reconcile would have: each is cut off and marked gone from now, so the reconcile erases them
INKCAST_KEYCLOAK_ERASE_AFTER_DAYS later (at its next pass with 0; never with off), after reading Keycloak again. One
who signed in since the pass read them is spared (Keycloak let them in: the one proof Inkcast has that it has them),
and one erased meanwhile (their own DELETE_ACCOUNT event) is in no pass any more. Dismissing does nothing to anyone;
the next reconcile reads everyone again and trips again if it must. Either is claimed once: a second confirmation, or
a dismissal after a confirmation, does nothing and says so. Each outcome is one log line, the audit record
(`identity.sync.breaker.confirmed`, `identity.sync.breaker.dismissed`: the pass id, who, and counts; never a person's
id), beside the pass's own row (`sync_breaker_trips`: status, time, who). Runs in the IdentityService pod, against the
same database and INKCAST_SECRET_KEY, while the server serves. Exit status: 0 done, 1 refused, 2 bad usage.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .libs.config import ConfigError, load_config
from .libs.db import Store
from .libs.db.identity import BREAKER_CONFIRMED, BREAKER_DISMISSED
from .libs.observability import setup_logging
from .services.identity.keycloak_sync import cut_off_person

log = logging.getLogger("inkcast.identity.sync")


def _when(at: float | None) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if at else "-"


def _who(given: str | None) -> str:
    if given:
        return given
    try:
        return getpass.getuser()
    except (KeyError, OSError):            # no account name for this uid (a container's arbitrary uid)
        return "operator"


def _store(environ: Mapping[str, str] | None) -> Store:
    config = load_config(environ)
    config.validate(("secret_key",))        # the erasure audit's salt and the sealer need the server's key
    return Store(config.database_path, config.secret_key)


def _trips(store: Store, out: Any) -> int:
    for trip in store.breaker_trips():
        line = f"{trip['id']}  {_when(trip['tripped_at'])}  {trip['gone']} of {trip['read']} gone  {trip['status']}"
        if trip["resolved_at"]:
            line += f" {_when(trip['resolved_at'])} by {trip['resolved_by']}"
        print(line, file=out)
    return 0


def _confirm(store: Store, pass_id: str, by: str, now: float) -> int:
    trip = store.get_breaker_trip(pass_id)
    if trip is None:
        log.error("No pass %s: `inkcast identity trips` lists them", pass_id, extra={"event": "identity.sync.breaker.refused"})
        return 1
    if not store.resolve_breaker_trip(pass_id, BREAKER_CONFIRMED, now, by):
        log.error("Pass %s was %s already (%s, by %s): nothing done", pass_id, trip["status"], _when(trip["resolved_at"]),
                  trip["resolved_by"], extra={"event": "identity.sync.breaker.refused"})
        return 1
    applied = skipped = 0
    for held in store.breaker_trip_people(pass_id):
        user = store.get_user(held["user_id"])
        if user is None or user.get("last_login_at") != held["last_login_at"]:
            skipped += 1                   # erased meanwhile, or Keycloak let them in since the pass read them
            continue
        cut_off_person(store, None, user, "deleted", "confirm", now)
        applied += 1
    store.forget_breaker_trip_people(pass_id)
    log.warning("Pass %s confirmed by %s: cut off %d, their grace period running from now; skipped %d (signed in since, "
                "or erased already)", pass_id, by, applied, skipped,
                extra={"event": "identity.sync.breaker.confirmed", "pass_id": pass_id, "by": by, "applied": applied,
                       "skipped": skipped})
    return 0


def _dismiss(store: Store, pass_id: str, by: str, now: float) -> int:
    trip = store.get_breaker_trip(pass_id)
    if trip is None:
        log.error("No pass %s: `inkcast identity trips` lists them", pass_id, extra={"event": "identity.sync.breaker.refused"})
        return 1
    if not store.resolve_breaker_trip(pass_id, BREAKER_DISMISSED, now, by):
        log.error("Pass %s was %s already (%s, by %s): nothing done", pass_id, trip["status"], _when(trip["resolved_at"]),
                  trip["resolved_by"], extra={"event": "identity.sync.breaker.refused"})
        return 1
    store.forget_breaker_trip_people(pass_id)
    log.warning("Pass %s dismissed by %s: nothing done to anyone; the next reconcile reads everyone again", pass_id, by,
                extra={"event": "identity.sync.breaker.dismissed", "pass_id": pass_id, "by": by})
    return 0


def identity_main(argv: list[str], environ: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time,
                  out: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="inkcast identity", description="Resolve a pass the Keycloak user sync holds.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("trips", help="list every pass that tripped the breaker, oldest first")
    for name, help_text in (("confirm-deletions", "the people of this pass really are gone: cut them off and start the clock"),
                            ("dismiss-deletions", "Keycloak was wrong about this pass: do nothing, read everyone again")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--pass", dest="pass_id", required=True, metavar="ID", help="the pass id (inkcast identity trips)")
        sub.add_argument("--by", default=None, metavar="WHO", help="who decided, for the audit line (default: your user name)")
    args = parser.parse_args(argv)
    settings = environ if environ is not None else os.environ
    # The listing is a table on stdout a script may read (`--pass $(... | cut)`): no INFO lines in front of it.
    setup_logging(settings.get("INKCAST_LOG_FORMAT", "text").lower(),
                  "WARNING" if args.command == "trips" else settings.get("INKCAST_LOG_LEVEL", "INFO"), service="Identity")
    try:
        store = _store(environ)
    except ConfigError as exc:
        print(f"inkcast identity: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command == "trips":
            return _trips(store, out or sys.stdout)
        if args.command == "confirm-deletions":
            return _confirm(store, args.pass_id, _who(args.by), clock())
        return _dismiss(store, args.pass_id, _who(args.by), clock())
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(identity_main(sys.argv[1:]))
