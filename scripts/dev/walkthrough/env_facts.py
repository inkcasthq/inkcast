#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The facts the walk document states about where it ran, measured at build time and written as JSON:
the git commit, Inkcast's version, the Python, Playwright and Chromium versions, the operating system.

    env_facts.py > env.json
"""

from __future__ import annotations

import json
import os
import pathlib
import platform
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))


def git_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get("GITHUB_SHA", "unknown")


def chromium_version() -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            return b.version
        finally:
            b.close()


def dist_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def main() -> int:
    from inkcast import __version__
    facts = {
        "git_sha": git_sha(),
        "git_ref": os.environ.get("GITHUB_REF_NAME") or subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip(),
        "inkcast_version": __version__,
        "python": platform.python_version(),
        "playwright": dist_version("playwright"),
        "mcp": dist_version("mcp"),
        "chromium": chromium_version(),
        "os": platform.platform(),
        "ci": os.environ.get("GITHUB_RUN_ID", ""),
    }
    print(json.dumps(facts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
