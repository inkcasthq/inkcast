#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""File the backlog (docs/backlog/*.yaml) as GitHub issues: one per epic, feature and story.

    scripts/backlog/file.py inkcasthq/inkcast --dry-run      # print what would be created
    scripts/backlog/file.py inkcasthq/inkcast                # create what's missing (needs `gh auth login`)
    scripts/backlog/file.py inkcasthq/inkcast --epic E3      # one epic only

Idempotent: an issue whose title already exists (open or closed) is skipped, so re-running after
editing the YAML only adds the new items. Stories marked `done` are created closed. Each issue
links to its epic's design document and its parent issue.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
LABELS = {"epic": "5319e7", "feature": "1d76db", "story": "0e8a16", "backlog-as-code": "cccccc"}


def gh(*args: str, dry: bool = False) -> str:
    if dry:
        print("  gh " + " ".join(args[:4]) + (" …" if len(args) > 4 else ""))
        return ""
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout.strip()


def load(epic: str | None) -> list[dict]:
    docs = [yaml.safe_load(p.read_text()) for p in sorted((ROOT / "docs" / "backlog").glob("E*.yaml"))]
    return [d for d in docs if not epic or d["id"] == epic]


def design_link(repo: str, epic: dict) -> str:
    folders = sorted((ROOT / "docs" / "epics").glob(f"{epic['id']}-*"))
    if folders:
        return f"https://github.com/{repo}/blob/main/docs/epics/{folders[0].name}/design.md"
    return f"https://github.com/{repo}/blob/main/docs/epics/README.md"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo")
    parser.add_argument("--epic")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    dry = args.dry_run

    existing: set[str] = set()
    if not dry:
        for name, color in LABELS.items():
            subprocess.run(["gh", "label", "create", name, "--repo", args.repo, "--color", color, "--force"],
                           capture_output=True)
        listed = gh("issue", "list", "--repo", args.repo, "--state", "all", "--limit", "2000", "--json", "title")
        existing = {i["title"] for i in json.loads(listed or "[]")}

    def create(title: str, body: str, labels: list[str], closed: bool) -> str:
        if title in existing:
            print(f"= {title}")
            return ""
        print(f"+ {title}")
        url = gh("issue", "create", "--repo", args.repo, "--title", title, "--body", body,
                 "--label", ",".join(labels + ["backlog-as-code"]), dry=dry)
        if closed and url:
            gh("issue", "close", url, "--reason", "completed", dry=dry)
        return url

    for epic in load(args.epic):
        link = design_link(args.repo, epic)
        epic_url = create(f"[{epic['id']}] {epic['title']}", f"{epic['goal']}\n\nDesign: {link}", ["epic"],
                          epic["status"] == "done")
        for feature in epic["features"]:
            parent = f"\n\nEpic: {epic_url}" if epic_url else ""
            f_url = create(f"[{feature['id']}] {feature['title']}", f"Part of {epic['id']} {epic['title']}.{parent}"
                           f"\n\nDesign: {link}", ["feature"], all(s["status"] == "done" for s in feature["stories"]))
            for story in feature["stories"]:
                parent = f"\n\nFeature: {f_url}" if f_url else ""
                create(f"[{story['id']}] {story['title']}",
                       f"Part of {feature['id']} {feature['title']} ({epic['id']}).{parent}\n\n"
                       f"Definition of done: https://github.com/{args.repo}/blob/main/docs/definition-of-done.md",
                       ["story"], story["status"] == "done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
