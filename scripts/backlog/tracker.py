#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Generate the backlog tracker, docs/backlog/TRACKER.md, from the backlog and its execution order.

    scripts/backlog/tracker.py                               # rewrite the tracker
    scripts/backlog/tracker.py --check                       # exit 1 if the order is invalid or the tracker is stale
    scripts/backlog/tracker.py --drift inkcasthq/inkcast     # compare with the GitHub issues (needs `gh`; not in CI)

The stories live in docs/backlog/E*.yaml and the order in docs/backlog/order.yaml. Every story that is not done must
appear in the order exactly once, so a new story cannot be filed without a place in the queue. Each wave names its
delivery phase (docs/epics/README.md, "Delivery phases"), and the stories in it must belong to that phase or be
ongoing work. Tests run --check.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKLOG = ROOT / "docs" / "backlog"
ORDER = BACKLOG / "order.yaml"
TRACKER = BACKLOG / "TRACKER.md"
REPO = "inkcasthq/inkcast"
STATUSES = ("done", "in-progress", "planned")
STORY_ID = re.compile(r"F\d+\.\d+\.S\d+")
ISSUE_REF = re.compile(r"#(\d+)")
# Lists of open issues outside the waves: developer tooling, and the issues that track the backlog itself.
ISSUE_LISTS = ("tooling", "tracking")
# Delivery phases: 1 to 5 are worked in order; ongoing work runs beside every phase.
PHASES = (1, 2, 3, 4, 5, "ongoing")


def load_epics(folder: pathlib.Path = BACKLOG) -> list[dict]:
    epics = [yaml.safe_load(p.read_text()) for p in folder.glob("E*.yaml")]
    return sorted(epics, key=lambda e: int(e["id"][1:]))


def index(epics: list[dict]) -> dict[str, tuple[dict, dict, dict]]:
    """Story id -> (epic, feature, story). A story id written twice is an error: a dict would keep one silently."""
    stories: dict[str, tuple[dict, dict, dict]] = {}
    for e in epics:
        for f in e["features"]:
            for s in f["stories"]:
                if s["id"] in stories:
                    raise ValueError(f"{s['id']} is in docs/backlog twice ({stories[s['id']][0]['id']} and {e['id']})")
                stories[s["id"]] = (e, f, s)
    return stories


def phase(feature: dict, story: dict):
    """A story's delivery phase: its own when it differs from its feature's, otherwise the feature's."""
    return story.get("phase", feature.get("phase"))


def validate(stories: dict[str, tuple[dict, dict, dict]], order: dict) -> list[str]:
    """Every problem with the backlog or the order, as one line each; empty when both are valid."""
    problems = []
    for sid, (_, _, story) in stories.items():
        if story.get("status") not in STATUSES:
            problems.append(f"{sid}: status {story.get('status')!r} is not one of {', '.join(STATUSES)}")
    seen: dict[str, str] = {}
    wave_ids = [w.get("id") for w in order["waves"]]
    if len(set(wave_ids)) != len(wave_ids):
        problems.append(f"wave ids repeat: {wave_ids}")
    last = None
    for w in order["waves"]:
        missing = [key for key in ("id", "title", "why") if not w.get(key)]
        if missing:
            problems.append(f"wave {w.get('id')!r} has no {', '.join(missing)}")
        if w.get("phase") not in PHASES:
            problems.append(f"wave {w.get('id')!r}: phase {w.get('phase')!r} is not one of {', '.join(map(str, PHASES))}")
        elif w["phase"] != "ongoing":
            if last is not None and w["phase"] < last[1]:
                problems.append(f"wave {w['id']!r} is phase {w['phase']}, after {last[0]!r} in phase {last[1]}")
            last = (w["id"], w["phase"])
    titled = {int(n) for n in order.get("issues") or {}}
    places = [(w.get("id"), item) for w in order["waves"] for item in w.get("items") or []]
    places += [(key, item) for key in ISSUE_LISTS for item in order.get(key) or []]
    for where, item in places:
        if not isinstance(item, str):
            # `- #342` without quotes is a YAML comment, so the item reads as None
            problems.append(f"{item!r} ({where}) is neither a story id nor '#N' (quote '#N' in YAML)")
            continue
        if item in seen:
            problems.append(f"{item} is placed twice ({seen[item]} and {where})")
        seen[item] = where
        if ISSUE_REF.fullmatch(item):
            if int(item[1:]) not in titled:
                problems.append(f"{item} ({where}) has no title under 'issues:'")
        elif where in ISSUE_LISTS:
            problems.append(f"{item!r} ({where}) is not '#N': {where} holds open issues only")
        elif not STORY_ID.fullmatch(item):
            problems.append(f"{item!r} ({where}) is neither a story id nor '#N'")
        elif item not in stories:
            problems.append(f"{item} ({where}) is not in docs/backlog/E*.yaml")
        else:
            wave_phase = next(w.get("phase") for w in order["waves"] if w.get("id") == where)
            story_phase = phase(stories[item][1], stories[item][2])
            if story_phase not in (wave_phase, "ongoing"):
                problems.append(f"{item} ({where}) is phase {story_phase!r}, but {where} is phase {wave_phase!r}")
    for sid, (_, _, story) in stories.items():
        if story.get("status") != "done" and sid not in seen:
            problems.append(f"{sid} is {story.get('status')} but has no place in docs/backlog/order.yaml")
    return problems


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _issue(number: int | None) -> str:
    return f"[#{number}](https://github.com/{REPO}/issues/{number})" if number else ""


def _count(stories: list[dict]) -> dict[str, int]:
    return {s: sum(1 for x in stories if x["status"] == s) for s in STATUSES}


def render(epics: list[dict], order: dict) -> str:
    stories = index(epics)
    titles = {int(n): t for n, t in (order.get("issues") or {}).items()}
    position: dict[str, str] = {}
    rows_by_wave = []
    n = 0
    for wave in order["waves"]:
        rows = []
        for item in wave.get("items") or []:      # a wave may be empty while its issues are closed
            n += 1
            position[item] = f"{n} ({wave['id']})"
            if ISSUE_REF.fullmatch(item):
                num = int(item[1:])
                rows.append(f"| {n} | {item} | open issue | {_cell(titles[num])} | | {_issue(num)} |")
            else:
                epic, _, story = stories[item]
                rows.append(f"| {n} | {item} | {story['status']} | {_cell(story['title'])} | {epic['id']} "
                            f"| {_issue(story.get('issue'))} |")
        rows_by_wave.append((wave, rows))

    every = [s for _, _, s in stories.values()]
    total = _count(every)
    out = [
        "# Backlog tracker",
        "",
        "<!-- GENERATED by scripts/backlog/tracker.py from docs/backlog/E*.yaml and docs/backlog/order.yaml. "
        "Do not edit. -->",
        "",
        "This page lists every story in the backlog and the order in which the work is done. To change a story, edit "
        "its epic file in `docs/backlog/`. To change the order, edit `docs/backlog/order.yaml`. Then run "
        "`scripts/backlog/tracker.py`. The tests fail when this page is out of date.",
        "",
        f"The backlog holds {len(every)} stories: {total['done']} done, {total['in-progress']} in progress and "
        f"{total['planned']} planned.",
        "",
        *[f"Tracked in {_issue(int(i[1:]))}: {_cell(titles[int(i[1:])])}." for i in order.get("tracking") or []],
        *([""] if order.get("tracking") else []),
        "## Progress by epic",
        "",
        "| Epic | Title | Done | In progress | Planned | Total |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for epic in epics:
        c = _count([s for f in epic["features"] for s in f["stories"]])
        out.append(f"| {epic['id']} | {_cell(epic['title'])} | {c['done']} | {c['in-progress']} | {c['planned']} "
                   f"| {sum(c.values())} |")
    out += ["", "## Execution order", "",
            "Work the waves from top to bottom, and the items in each wave in the order listed. An item is a story, "
            "or an open issue that is not a story. Each wave names its delivery phase (see the epics index): the "
            "phases run 1 to 5 in order, and the ongoing waves are worked beside them.", ""]
    for wave, rows in rows_by_wave:
        in_wave = [i for i in wave.get("items") or [] if i in stories]      # the open issues are not stories
        done = sum(1 for i in in_wave if stories[i][2]["status"] == "done")
        out += [f"### {wave['id']}: {_cell(wave['title'])} (phase {wave['phase']}, {done} of {len(in_wave)} stories done)",
                "",
                _cell(wave["why"]), "",
                "| # | Item | Status | Title | Epic | Issue |", "|---:|---|---|---|---|---|", *rows, ""]
    if order.get("tooling"):
        out += ["### Developer tooling", "", "Outside the product. Worked only when the owner asks.", "",
                "| Item | Title |", "|---|---|"]
        out += [f"| {_issue(int(i[1:]))} | {_cell(titles[int(i[1:])])} |" for i in order["tooling"]]
        out.append("")
    out += ["## By epic", ""]
    for epic in epics:
        c = _count([s for f in epic["features"] for s in f["stories"]])
        out += [f"### {epic['id']}: {_cell(epic['title'])} ({c['done']} of {sum(c.values())} done)", "",
                "| Story | Status | Title | Issue | Order |", "|---|---|---|---|---|"]
        for feature in epic["features"]:
            for s in feature["stories"]:
                out.append(f"| {s['id']} | {s['status']} | {_cell(s['title'])} | {_issue(s.get('issue'))} "
                           f"| {position.get(s['id'], '')} |")
        out.append("")
    return "\n".join(out)


def drift(repo: str, stories: dict[str, tuple[dict, dict, dict]], order: dict) -> list[str]:
    """Where the backlog and the order disagree with the GitHub issues."""
    listed = subprocess.run(["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "5000",
                             "--json", "number,title,state"], check=True, capture_output=True, text=True).stdout
    issues = {i["number"]: i for i in json.loads(listed)}
    problems = []
    filed: dict[str, int] = {}
    for number, issue in sorted(issues.items()):
        m = re.match(r"\[(F\d+\.\d+\.S\d+)\]", issue["title"])
        if m and m.group(1) in filed:
            problems.append(f"issues #{filed[m.group(1)]} and #{number} are both titled [{m.group(1)}]")
        elif m:
            filed[m.group(1)] = number
    titles = {int(n): t for n, t in (order.get("issues") or {}).items()}
    for number, title in sorted(titles.items()):
        if number in issues and issues[number]["title"] != title:
            problems.append(f"#{number}: order.yaml's title {title!r} is not GitHub's {issues[number]['title']!r}")
    for sid, (_, _, story) in stories.items():
        number = story.get("issue")
        if sid in filed and number != filed[sid]:
            problems.append(f"{sid}: issue #{filed[sid]} is titled [{sid}], but the backlog says issue: {number}")
        if number is None:
            continue
        issue = issues.get(number)
        if issue is None:
            problems.append(f"{sid}: issue #{number} does not exist")
        elif (issue["state"] == "CLOSED") != (story["status"] == "done"):
            problems.append(f"{sid}: the backlog says {story['status']}, issue #{number} is {issue['state'].lower()}")
    for sid, number in sorted(filed.items()):
        if sid not in stories:
            problems.append(f"issue #{number} is titled [{sid}], which is not in docs/backlog/E*.yaml")
    placed = {int(i[1:]) for w in order["waves"] for i in w.get("items") or [] if ISSUE_REF.fullmatch(i)}
    placed |= {int(i[1:]) for key in ISSUE_LISTS for i in order.get(key) or []}
    for number in sorted(placed):
        issue = issues.get(number)
        if issue is None:
            problems.append(f"#{number} is in docs/backlog/order.yaml but does not exist")
        elif issue["state"] != "OPEN":
            problems.append(f"#{number} is in docs/backlog/order.yaml but is {issue['state'].lower()}")
    story_issues = set(filed.values()) | {s["issue"] for _, _, s in stories.values() if s.get("issue")}
    for number, issue in sorted(issues.items()):
        if issue["state"] == "OPEN" and number not in placed and number not in story_issues:
            problems.append(f"#{number} is open but has no place in docs/backlog/order.yaml: {issue['title']}")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--drift", metavar="REPO")
    args = parser.parse_args(argv)
    epics, order = load_epics(), yaml.safe_load(ORDER.read_text())
    try:
        stories = index(epics)
    except ValueError as exc:
        print(f"The backlog or its order is invalid:\n  {exc}", file=sys.stderr)
        return 1
    problems = validate(stories, order)
    if problems:
        print("The backlog or its order is invalid:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    if args.drift:
        found = drift(args.drift, stories, order)
        print("\n".join(found) if found else "The backlog, the order and the GitHub issues agree.")
        return 1 if found else 0
    text = render(epics, order) + "\n"
    if args.check:
        if not TRACKER.exists() or TRACKER.read_text() != text:
            print("Out of date (run scripts/backlog/tracker.py): docs/backlog/TRACKER.md", file=sys.stderr)
            return 1
        return 0
    TRACKER.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
