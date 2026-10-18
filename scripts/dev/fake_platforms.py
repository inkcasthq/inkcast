#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Serve the test suite's fake DEV and Medium APIs (tests/fakes.py) so a local stack can publish for real.

    python scripts/dev/fake_platforms.py            # http://0.0.0.0:8999 (FAKES_PORT, FAKES_PUBLIC_URL)

Used by deploy/dev (Docker Compose and kind) to give the dashboards real publish traffic without touching a
real platform. Point Inkcast at it with INKCAST_API_URL_DEVTO=<url>/devto/api and
INKCAST_API_URL_MEDIUM=<url>/medium/v1. The fakes accept the API key `devto-key` and the token `medium-token`.
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
    uvicorn.run(Fakes(public).app(), host=os.environ.get("FAKES_HOST", "0.0.0.0"), port=port, log_level="warning")


if __name__ == "__main__":
    main()
