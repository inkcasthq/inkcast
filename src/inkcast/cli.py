# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``inkcast sync``: publish markdown files from a repository, from any CI or a laptop.

    export INKCAST_URL=https://inkcast.example.com
    export INKCAST_TOKEN=ink_pat_...          # a personal access token with the stories:sync scope
    inkcast sync 'posts/**/*.md'              # publish new files, update changed ones, skip the rest
    inkcast sync --dry-run 'posts/**/*.md'    # say what would happen, with per-platform previews

The token is read from an environment variable only (never a command-line argument, which other
users of the machine could see). Files are sent in batches of 50; each batch carries an
Idempotency-Key, so a retried batch can never publish twice. In GitHub Actions the repository,
commit and image URLs are detected automatically, results go to the job summary, and the step
outputs ``results`` (JSON) and ``urls``.

Exit codes: 0 all good, 1 something failed, 2 usage or configuration problem.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import httpx2

BATCH = 50
ICONS = {"create": "+", "update": "~", "unchanged": "=", "stale": "!", "skipped": "-", "failed": "x"}


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def detect_source() -> str:
    """``github.com/owner/repo``: from GitHub Actions, else the git remote."""
    if os.environ.get("GITHUB_REPOSITORY"):
        host = os.environ.get("GITHUB_SERVER_URL", "https://github.com").split("://", 1)[-1]
        return f"{host}/{os.environ['GITHUB_REPOSITORY']}"
    remote = _git("remote", "get-url", "origin")
    if remote.startswith("git@"):
        remote = remote[4:].replace(":", "/", 1)
    remote = remote.split("://", 1)[-1].split("@", 1)[-1]
    return remote[:-4] if remote.endswith(".git") else remote


def detect_ref() -> str | None:
    return os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD") or None


def raw_base(source: str, ref: str | None) -> str | None:
    """Where relative images in the repository can be fetched publicly (GitHub only)."""
    host, _, repo = source.partition("/")
    if not ref or host != "github.com" or repo.count("/") != 1:
        return None
    return f"https://raw.githubusercontent.com/{repo}/{ref}/"


def collect(patterns: list[str]) -> list[str]:
    found: list[str] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=True)) or ([pattern] if os.path.isfile(pattern) else [])
        for m in matches:
            p = pathlib.PurePosixPath(pathlib.Path(m).as_posix())
            if p.suffix.lower() in (".md", ".markdown") and str(p) not in found:
                found.append(str(p))
    return found


def build_requests(paths: list[str], source: str, ref: str | None, platforms: list[str] | None,
                   image_base: str | None, dry_run: bool) -> list[dict[str, Any]]:
    batches = []
    for i in range(0, len(paths), BATCH):
        files = []
        for path in paths[i:i + BATCH]:
            directory = str(pathlib.PurePosixPath(path).parent)
            base = (image_base.rstrip("/") + "/" + ("" if directory == "." else directory + "/")) if image_base else None
            files.append({"path": path, "markdown": pathlib.Path(path).read_text(encoding="utf-8"),
                          "image_base_url": base})
        batches.append({"source": source, "ref": ref, "files": files, "platforms": platforms, "dry_run": dry_run})
    return batches


def render_table(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        line = f"{ICONS.get(r['action'], '?')} {r['action']:<9} {r['platform']:<8} {r['path']}"
        if r.get("url"):
            line += f"  {r['url']}"
        if r.get("message"):
            line += f"\n    {r['message']}"
        lines.append(line)
    return "\n".join(lines)


def render_summary(results: list[dict[str, Any]], dry_run: bool) -> str:
    out = ["### Inkcast " + ("dry run" if dry_run else "sync"), "", "| | File | Platform | Result | Link |",
           "|---|---|---|---|---|"]
    for r in results:
        link = f"[open]({r['url']})" if r.get("url") else ""
        note = f" — {r['message']}" if r.get("message") else ""
        out.append(f"| {ICONS.get(r['action'], '?')} | `{r['path']}` | {r['platform']} | {r['action']}{note} | {link} |")
    return "\n".join(out) + "\n"


def sync_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="inkcast sync", description="Publish markdown files from a repository.")
    parser.add_argument("paths", nargs="+", help="files or globs, e.g. 'posts/**/*.md'")
    parser.add_argument("--url", default=os.environ.get("INKCAST_URL", ""), help="Inkcast URL (env INKCAST_URL)")
    parser.add_argument("--token-env", default="INKCAST_TOKEN", help="environment variable holding the token")
    parser.add_argument("--source", default=None, help="repository id; default: detected, e.g. github.com/acme/blog")
    parser.add_argument("--ref", default=None, help="commit being synced; default: detected")
    parser.add_argument("--platforms", default=None, help="comma-separated; overrides each file's front matter")
    parser.add_argument("--image-base-url", default=None,
                        help="public base URL of the repository root for relative images; default: raw GitHub URL")
    parser.add_argument("--dry-run", action="store_true", help="report and preview only; change nothing")
    parser.add_argument("--fail-on-stale", action="store_true", help="exit 1 if a changed file couldn't be updated")
    parser.add_argument("--json", action="store_true", help="print the raw results as JSON")
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env, "").strip()
    if not args.url or not token:
        print(f"inkcast sync: set INKCAST_URL (or --url) and {args.token_env}.", file=sys.stderr)
        return 2
    if not token.startswith("ink_pat_"):
        print(f"inkcast sync: {args.token_env} doesn't look like a personal access token (ink_pat_...).", file=sys.stderr)
        return 2
    paths = collect(args.paths)
    if not paths:
        print("inkcast sync: no markdown files matched.", file=sys.stderr)
        return 0
    source = args.source or detect_source()
    if not source:
        print("inkcast sync: can't tell which repository this is; pass --source.", file=sys.stderr)
        return 2
    ref = args.ref or detect_ref()
    image_base = args.image_base_url or raw_base(source, ref)
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()] if args.platforms else None

    results: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "inkcast-sync"}
    with httpx2.Client(base_url=args.url.rstrip("/"), timeout=120, headers=headers) as client:
        for body in build_requests(paths, source, ref, platforms, image_base, args.dry_run):
            key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            try:
                r = client.post("/api/v1/stories:sync", json=body, headers={"Idempotency-Key": key})
            except httpx2.HTTPError as exc:
                print(f"inkcast sync: can't reach {args.url}: {exc}", file=sys.stderr)
                return 1
            if r.status_code != 200:
                try:
                    detail = r.json().get("message") or r.text
                except ValueError:
                    detail = r.text
                print(f"inkcast sync: {r.status_code}: {detail}", file=sys.stderr)
                return 2 if r.status_code in (401, 403) else 1
            results += r.json()["results"]

    print(json.dumps(results, indent=2) if args.json else render_table(results))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(render_summary(results, args.dry_run))
    if os.environ.get("GITHUB_OUTPUT"):
        urls = " ".join(r["url"] for r in results if r.get("url"))
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"results={json.dumps(results, separators=(',', ':'))}\nurls={urls}\n")
    failed = any(r["action"] == "failed" for r in results)
    stale = any(r["action"] == "stale" for r in results)
    return 1 if failed or (stale and args.fail_on_stale) else 0
