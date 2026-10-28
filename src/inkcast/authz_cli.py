# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``inkcast authz write-model`` (E3.F3.9.S1, issue #279): the store and the model version a deployment pins.

    inkcast authz write-model                       # the packaged model, the store INKCAST_FGA_STORE_NAME names
    inkcast authz write-model --store NAME --model PATH

Reads ``INKCAST_FGA_API_URL``, the store's name ``INKCAST_FGA_STORE_NAME`` (``inkcast`` when unset; ``--store``
wins) and the preshared key ``INKCAST_FGA_API_TOKEN`` (or ``_FILE``) through the secrets design. Creates the store
when no store of that name exists; writes the model as a new version unless the store's newest version already is
this model (``same_model``), so a second run changes nothing. Prints, on stdout and nothing else there::

    INKCAST_FGA_STORE_ID=01...
    INKCAST_FGA_MODEL_ID=01...

so ``eval "$(inkcast authz write-model)"`` or a pull request can pin both (docs/authorization.md §8). With both
``INKCAST_FGA_STORE_ID`` and ``INKCAST_FGA_MODEL_ID`` set, a version is held by hand and wins at start-up: it prints
them, writes nothing and calls nothing.

The deployment runs it as the init container ``authz-model`` of every Inkcast pod that checks permissions (E3.F3.9.S6,
components/openfga/authz-model.yaml), before the server, which then pins the same store and version by name: nothing
is pinned by hand. Pods starting together on a fresh install converge on one store (``_write_model``). What happened
(store created, found or deleted, model written or unchanged) goes to stderr. The key is never printed. Exit 0 when
the ids are printed, 1 on a configuration or OpenFGA problem, with the reason on stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any, Mapping

import httpx2

from .libs.authz.model import PACKAGED_MODEL
from .libs.authz.openfga import OpenFgaAdmin, same_model
from .libs.authz.types import EngineError, InvalidRequest
from .libs.config import ConfigError, secrets_provider, settings_from
from .libs.observability import setup_logging
from .libs.secrets import NamingError, SecretFetchError

DEFAULT_STORE = "inkcast"


def authz_main(argv: list[str], environ: Mapping[str, str] | None = None, out: Any = None, err: Any = None,
               http: httpx2.AsyncClient | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inkcast authz", description="The OpenFGA store Inkcast checks permissions in.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write-model", help="create the store if it is missing, write the model once, and print "
                                                    "INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID to pin")
    write.add_argument("--store", default=None, metavar="NAME",
                       help=f"the store's name (default: INKCAST_FGA_STORE_NAME, or {DEFAULT_STORE})")
    write.add_argument("--model", default=None, metavar="PATH",
                       help="a compiled model (authz/model.json) to write instead of the packaged one")
    args = parser.parse_args(argv)
    out, err = out or sys.stdout, err or sys.stderr
    settings = settings_from(environ if environ is not None else os.environ)
    pinned = [settings.get(name, "").strip() for name in ("INKCAST_FGA_STORE_ID", "INKCAST_FGA_MODEL_ID")]
    if all(pinned):   # the start-up rule (libs/authz pin_model): both ids set win, and nothing is resolved or written
        print("nothing to do: INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID pin a version by hand, and win over the "
              "store's name; no store is created and no model written (unset both to write one)", file=err)
        print(f"INKCAST_FGA_STORE_ID={pinned[0]}", file=out)
        print(f"INKCAST_FGA_MODEL_ID={pinned[1]}", file=out)
        return 0
    # The ids are the output a script reads: no log line in front of them (setup_logging writes to stdout).
    setup_logging(settings.get("INKCAST_LOG_FORMAT", "text").lower(), "WARNING", service="Authz")
    try:
        url, token, timeout = _fga_settings(settings)
        model = _load_model(pathlib.Path(args.model) if args.model else PACKAGED_MODEL)
    except ConfigError as exc:
        print(f"inkcast authz: {exc}", file=err)
        return 1
    admin = OpenFgaAdmin(url, token, http=http, timeout=timeout)
    store_name = args.store or settings.get("INKCAST_FGA_STORE_NAME", "").strip() or DEFAULT_STORE   # the server's name too

    async def run() -> tuple[str, str, list[str]]:
        try:
            return await _write_model(admin, store_name, model)
        finally:
            if http is None:   # the client this command opened closes on the loop that used it
                await admin.http.aclose()

    try:
        store_id, model_id, notes = asyncio.run(run())
    except (EngineError, InvalidRequest) as exc:
        print(f"inkcast authz: {exc}", file=err)
        return 1
    for note in notes:
        print(note, file=err)
    print(f"INKCAST_FGA_STORE_ID={store_id}", file=out)
    print(f"INKCAST_FGA_MODEL_ID={model_id}", file=out)
    return 0


def _fga_settings(settings: Mapping[str, str]) -> tuple[str, Any, float]:
    url = settings.get("INKCAST_FGA_API_URL", "").strip().rstrip("/")
    if not url:
        raise ConfigError("INKCAST_FGA_API_URL is not set: the OpenFGA server to write to (http://openfga:8080 in the "
                          "component, or a port-forward to it).")
    try:
        timeout = float(settings.get("INKCAST_FGA_TIMEOUT", "").strip() or "5")   # unset or empty: the default
    except ValueError:
        timeout = 0.0
    if not 0 < timeout < float("inf"):
        raise ConfigError("INKCAST_FGA_TIMEOUT must be a positive number of seconds.")
    provider = secrets_provider(settings, service=settings.get("INKCAST_APP_NAME", "All"))
    try:
        token = provider.get("INKCAST_FGA_API_TOKEN")  # empty when OpenFGA runs without authentication
    except (OSError, SecretFetchError, NamingError) as exc:
        raise ConfigError(f"INKCAST_FGA_API_TOKEN can't be read: {exc}") from exc
    return url, token, timeout


def _load_model(path: pathlib.Path) -> dict[str, Any]:
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"authorization model {path} can't be loaded: {exc}") from exc
    if not isinstance(model, dict) or not isinstance(model.get("type_definitions"), list) or not model["type_definitions"]:
        raise ConfigError(f"authorization model {path} is not a compiled model: it needs type_definitions "
                          "(fga model transform --file authz/model.fga --output-format json)")
    if model.get("schema_version") != "1.1":
        raise ConfigError(f"authorization model {path} has schema_version {model.get('schema_version')!r}; Inkcast's "
                          "engines evaluate 1.1")
    return model


async def _write_model(admin: OpenFgaAdmin, store_name: str, model: dict[str, Any]) -> tuple[str, str, list[str]]:
    """(store id, model id, what happened). Idempotent: the second run finds the store and sees the newest model is
    this one, and writes nothing.

    Convergent (E3.F3.9.S6): every Inkcast pod runs this before its server, so pods starting together on a fresh
    install may each find no store and each create one (find-then-create is not atomic, and OpenFGA does not keep
    names unique). A writer that created a store lists the stores of the name again; every writer uses the oldest
    (``OpenFgaAdmin.stores``), and one whose own store is not the oldest deletes it while it holds no version, so no
    server ever pins it. A writer that created none deletes every younger store that holds no version (one whose
    racer's delete, or create answer, was lost): OpenFGA writes a tuple only under a version (server/write.go), so it
    holds none, and no server pins it. A store holding a version is never deleted: it may hold tuples. Two stores
    that remain (made by hand, or a create whose older id lands after a younger store's writer has listed) are
    refused by the server's start-up, which names both."""
    notes = []
    stores = await admin.stores(store_name)
    created = None
    if not stores:
        created = await admin.create_store(store_name)
        notes.append(f"store {store_name!r} created: {created}")
        stores = sorted({*await admin.stores(store_name), created})   # with the racers' stores that landed meanwhile
    store_id = stores[0]
    # Only while it holds no version: a writer that found it alone (its older racer's row not landed yet) may have
    # written one, and its server pinned it; OpenFGA's delete is soft, so that server would go on in a hidden store.
    if created is not None and created != store_id and await admin.newest_model(created) is None:
        await admin.delete_store(created)
        notes.append(f"store {created} deleted: {len(stores)} stores named {store_name!r} were created at once, and "
                     f"every writer uses the oldest, {store_id}")
    elif created is not None and created != store_id:
        notes.append(f"store {created} kept: another writer wrote a version into it; Inkcast refuses to start until "
                     f"one store named {store_name!r} is left")
    elif created is None:
        empty = [other for other in stores[1:] if await admin.newest_model(other) is None]
        for other in empty:
            await admin.delete_store(other)
            notes.append(f"store {other} deleted: it holds no version, and every writer uses the oldest store named "
                         f"{store_name!r}, {store_id}")
        stores = [other for other in stores if other not in empty]
        notes.append(f"store {store_name!r} found: {store_id}" + ("" if len(stores) == 1 else
                     f" (the oldest of {len(stores)} stores named {store_name!r}: {', '.join(stores)}; a writer "
                     "deletes no store that holds a version, and Inkcast refuses to start until one is left)"))
    newest = await admin.newest_model(store_id)
    if newest is not None and same_model(newest, model):
        model_id = str(newest["id"])
        notes.append(f"model unchanged: the store's newest version {model_id} is this model")
    else:
        model_id = await admin.write_model(store_id, model)
        notes.append(f"model written: version {model_id}" + ("" if newest is None else f" (the newest was {newest['id']})"))
    return store_id, model_id, notes


if __name__ == "__main__":  # pragma: no cover
    sys.exit(authz_main(sys.argv[1:]))
