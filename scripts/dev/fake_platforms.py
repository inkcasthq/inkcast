#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Serve the test suite's fake DEV and Medium APIs (tests/fakes.py) so a local stack can publish for real.

    python scripts/dev/fake_platforms.py            # http://0.0.0.0:8999 (FAKES_PORT, FAKES_PUBLIC_URL)

Used by deploy/dev (Docker Compose and kind) to give the dashboards real publish traffic without touching a
real platform. Point Inkcast at it with INKCAST_API_URL_DEVTO=<url>/devto/api and
INKCAST_API_URL_MEDIUM=<url>/medium/v1. The fakes accept the API key `devto-key` and the token `medium-token`.

The same process serves the fake Keycloak (issuer <url>/kc/realms/inkcast, client `inkcast` / `kc-secret`, the
sync service account `inkcast-sync` / `sync-secret`). With FAKES_KC_ACCOUNT_EMAIL and FAKES_KC_ACCOUNT_PASSWORD
set it shows a login form for that one person (the walkthrough signs in through it); FAKES_KC_BASE_URL is the
client's baseUrl, where Keycloak's "deleted" page links back after an account deletion.
Development and CI only: it keeps everything in memory and checks nothing else.
"""

from __future__ import annotations

import os
import pathlib
import sys

import uvicorn

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from fakes import Fakes  # noqa: E402


def main() -> None:
    port = int(os.environ.get("FAKES_PORT", "8999"))
    public = os.environ.get("FAKES_PUBLIC_URL", f"http://127.0.0.1:{port}")
    fakes = Fakes(public)
    email, password = os.environ.get("FAKES_KC_ACCOUNT_EMAIL"), os.environ.get("FAKES_KC_ACCOUNT_PASSWORD")
    if email and password:
        fakes.kc_accounts = {email: (password, {**fakes.next_user, "email": email, "preferred_username": email.split("@")[0]})}
    fakes.kc_base_url = os.environ.get("FAKES_KC_BASE_URL") or None
    uvicorn.run(fakes.app(), host=os.environ.get("FAKES_HOST", "0.0.0.0"), port=port, log_level="warning")


if __name__ == "__main__":
    main()
