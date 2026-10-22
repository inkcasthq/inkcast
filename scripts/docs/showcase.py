#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Refresh docs/screenshots.md from a walkthrough run: a few pictures that show the product, not all of them.

    scripts/dev/walkthrough/run_walk.sh --out /tmp/walk     # or CI's ci-captures branch, walkthrough/latest/
    python3 scripts/docs/showcase.py --from /tmp/walk

Each shot is found by the walk's own step name (``SHOTS``), so new steps in the walk don't shift the selection.
Pictures are cropped to their content (the page background trimmed, a margin kept) and written to
docs/images/showcase/. The page names the commit and the date of the run. The walk has already masked every
generated credential and its integrity check scanned the pictures. Needs Pillow (a dev tool, not a dependency).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
IMAGES = REPO / "docs" / "images" / "showcase"
PAGE = REPO / "docs" / "screenshots.md"
MARGIN = 48  # pixels of background kept around the content, at the capture's 2x scale

# (the walk's step name, which occurrence, the file written, the heading, what it shows)
SHOTS = [
    ("consent page", 1, "consent.png", "Add Inkcast to your AI assistant",
     "Any MCP client signs in with OAuth (discovery, dynamic registration, PKCE). The person sees who is asking and "
     "what it will be able to do, and approves it in the browser."),
    ("connect page", 1, "connect.png", "Connect a platform, without the key going through chat",
     "The assistant hands over a one-time link; the API key is pasted into Inkcast's own page, encrypted on the "
     "server, and never shown to the assistant."),
    ("MCP inkcast_publish", 1, "dry-run.png", "A dry run first",
     "One markdown file with front matter. The dry run shows each platform's version of the story and any "
     "warnings; nothing is sent."),
    ("MCP inkcast_posts", 2, "posts.png", "Drafts and schedules",
     "A draft on DEV to check before it goes live, next to a post scheduled for later in the hour (which can still "
     "be cancelled)."),
    ("token created", 1, "token.png", "A token for publishing from Git",
     "A personal access token for the blog repository's GitHub Action, scoped and shown once (masked here)."),
    ("api redoc", 1, "api-reference.png", "The REST API reference",
     "Every deployment serves its OpenAPI reference at /api/redoc, with Swagger UI at /api."),
    ("deletion page", 1, "delete-account.png", "Delete my account",
     "What goes and what stays, stated before anything happens. The sign-in provider asks for the password again "
     "and confirms; Inkcast then erases the person's data."),
    ("account deleted", 1, "account-deleted.png", "Deleted",
     "Every token stops working at once; articles already published stay on the platforms."),
]


def pick(results: dict, step: str, occurrence: int) -> dict:
    found = [s for s in results["steps"] if s.get("step") == step and s.get("screenshot")]
    if len(found) < occurrence:
        raise SystemExit(f"the walk has no picture for step {step!r} (occurrence {occurrence})")
    return found[occurrence - 1]


def crop(src: pathlib.Path, dest: pathlib.Path) -> tuple[int, int]:
    from PIL import Image, ImageChops

    image = Image.open(src).convert("RGB")
    background = Image.new("RGB", image.size, image.getpixel((2, 2)))
    box = ImageChops.difference(image, background).convert("L").point(lambda v: 255 if v > 6 else 0).getbbox()
    if box:
        left, top, right, bottom = box
        image = image.crop((max(0, left - MARGIN), max(0, top - MARGIN),
                            min(image.width, right + MARGIN), min(image.height, bottom + MARGIN)))
    image.save(dest, optimize=True)
    return image.size


def page(env: dict, started: str, shots: list[tuple[str, str, str, int]]) -> str:
    sha = env.get("git_sha", "")[:12] or "unknown"
    lines = [
        "# Screenshots",
        "",
        "A few pictures of Inkcast in use, in the order a person meets them. They come from the "
        "[end-to-end walkthrough](walkthrough.md), which drives the whole journey for real (an MCP client, a browser, "
        "the REST API) and checks every step; the panels are labelled MCP tool calls, not any client's interface.",
        "",
        f"From the run at commit `{sha}` on {started[:10]}. The full walkthrough, every step pictured, is on the "
        "[`ci-captures`](https://github.com/inkcasthq/inkcast/tree/ci-captures/walkthrough/latest) branch, rebuilt "
        "by CI on every merge. Refresh this page with `python3 scripts/docs/showcase.py --from <walk output>`.",
        "",
    ]
    for n, (name, title, text, width) in enumerate(shots, 1):
        lines += [f"## {n}. {title}", "", text, "",
                  f'<img src="images/showcase/{name}" alt="{title}" width="{width}">', ""]
    lines += [
        "---",
        "<!-- inkcast-nav -->",
        "[Docs](README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · "
        "[Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="run", required=True, type=pathlib.Path, help="a walkthrough output directory")
    run = parser.parse_args().run
    results = json.loads((run / "results.json").read_text())
    env = json.loads((run / "env.json").read_text()) if (run / "env.json").exists() else {}
    if not all(s.get("ok") for s in results["steps"]):
        sys.exit("the walk did not pass; refusing to publish its pictures")
    IMAGES.mkdir(parents=True, exist_ok=True)
    for old in IMAGES.glob("*.png"):
        old.unlink()
    shots = []
    for step, occurrence, name, title, text in SHOTS:
        width, _ = crop(run / pick(results, step, occurrence)["screenshot"], IMAGES / name)
        shots.append((name, title, text, min(width // 2, 760)))  # the capture is 2x: show it at 1x, capped
        print("wrote", (IMAGES / name).relative_to(REPO))
    PAGE.write_text(page(env, results.get("started", ""), shots))
    print("wrote", PAGE.relative_to(REPO))


if __name__ == "__main__":
    main()
