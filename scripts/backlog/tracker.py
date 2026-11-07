#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Generate the backlog tracker, docs/backlog/TRACKER.md, from the backlog and its execution order.

    scripts/backlog/tracker.py                               # rewrite the tracker
    scripts/backlog/tracker.py --check                       # exit 1 if the order is invalid or the tracker is stale
    scripts/backlog/tracker.py --drift inkcasthq/inkcast     # compare with the GitHub issues (needs `gh`; not in CI)
    scripts/backlog/tracker.py --milestones inkcasthq/inkcast  # make GitHub's milestones match the order (needs `gh`)

The stories live in docs/backlog/E*.yaml and the order in docs/backlog/order.yaml. Every story that is not done must
appear in the order exactly once, so a new story cannot be filed without a place in the queue. Each wave names its
delivery phase (docs/epics/README.md, "Delivery phases"), and the stories in it must belong to that phase or be
ongoing work. Tests run --check.

Each phase is one GitHub milestone, defined in the order's `milestones:`. An issue's place gives its milestone: GitHub's
issue forms have no milestone key, so a template cannot set one (#571). --drift reports a difference, and --milestones
writes what it reports.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from typing import NamedTuple

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
    milestones = order.get("milestones") or {}
    for p in PHASES:
        missing = [key for key in ("title", "description") if not (milestones.get(p) or {}).get(key)]
        if missing:
            problems.append(f"phase {p!r} has no milestone {' or '.join(missing)} under 'milestones:'")
    for p in milestones:
        if p not in PHASES:
            problems.append(f"milestone {p!r} is not one of the phases {', '.join(map(str, PHASES))}")
    titles = [m.get("title") for m in milestones.values() if m and m.get("title")]
    if len(set(titles)) != len(titles):
        problems.append(f"milestone titles repeat: {titles}")
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
    out += ["", "## Milestones", "",
            "Each delivery phase is one GitHub milestone, a release with one goal. A milestone counts the issues and pull "
            "requests that exist, and most planned stories have no issue yet, so this page is the count of stories.", "",
            "| Phase | Milestone | Goal |", "|---|---|---|"]
    out += [f"| {p} | {_cell(order['milestones'][p]['title'])} | {_cell(order['milestones'][p]['description'])} |"
            for p in PHASES]
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


def wanted_milestones(stories: dict[str, tuple[dict, dict, dict]], order: dict) -> dict[int, str]:
    """Issue number -> the title of the milestone its place gives it: its wave's phase, or a done story's own phase once
    it has left the order. A closed '#N' leaves the order too and nothing here names it, so it keeps the milestone it
    has, and the milestone's count of closed issues stays true. An issue under ISSUE_LISTS is outside the releases."""
    defined = order["milestones"]
    wanted: dict[int, str] = {}
    for _, feature, story in stories.values():
        if story.get("issue") and phase(feature, story) in defined:
            wanted[story["issue"]] = defined[phase(feature, story)]["title"]
    for wave in order["waves"]:
        for item in wave.get("items") or []:
            number = int(item[1:]) if ISSUE_REF.fullmatch(item) else stories[item][2].get("issue")
            if number:
                wanted[number] = defined[wave["phase"]]["title"]
    return wanted


class MilestoneChange(NamedTuple):
    """One write that makes GitHub match the order. ``number`` is the issue or pull request (they share an endpoint), and
    ``milestone`` the title it takes, or None to clear it."""

    says: str
    title: str | None = None
    description: str | None = None
    number: int | None = None
    milestone: str | None = None


def milestone_changes(order: dict, wanted: dict[int, str], existing: list[dict], issues: list[dict],
                      prs: list[dict]) -> tuple[list[MilestoneChange], list[str]]:
    """What differs from the order, as GitHub's ``existing`` milestones, ``issues`` and open ``prs`` read: ``(the writes
    that fix it, what only a person can settle)``. A milestone the order does not define is only reported: a title
    changed in the order leaves the old one holding its issues, and deleting it is a person's decision. Due dates and
    open or closed are never compared, so a date the owner sets and a milestone closed at its release stay."""
    current = {m["title"]: m for m in existing}
    changes = []
    for definition in order["milestones"].values():
        title, description = definition["title"], definition["description"]
        if title not in current:
            changes.append(MilestoneChange(f"milestone {title!r} does not exist", title=title, description=description))
        elif (current[title].get("description") or "") != description:
            changes.append(MilestoneChange(f"milestone {title!r}: its description differs from order.yaml",
                                           title=title, description=description))
    defined = {m["title"] for m in order["milestones"].values()}
    settle = [f"milestone {title!r} is on GitHub but not in order.yaml" for title in sorted(set(current) - defined)]

    def has(item: dict) -> str | None:
        return (item.get("milestone") or {}).get("title")

    outside = {int(i[1:]) for key in ISSUE_LISTS for i in order.get(key) or []}
    for issue in sorted(issues, key=lambda i: i["number"]):
        number, actual = issue["number"], has(issue)
        if number in wanted and actual != wanted[number]:
            changes.append(MilestoneChange(f"#{number}: milestone {actual!r}, expected {wanted[number]!r}", number=number,
                                           milestone=wanted[number]))
        elif number in outside and actual is not None:
            changes.append(MilestoneChange(f"#{number}: milestone {actual!r}, but it is outside the releases", number=number))
    for pr in sorted(prs, key=lambda p: p["number"]):
        closes = sorted(r["number"] for r in pr.get("closingIssuesReferences") or [] if r["number"] in wanted)
        if closes and has(pr) not in {wanted[n] for n in closes}:
            changes.append(MilestoneChange(f"pull request #{pr['number']}: milestone {has(pr)!r}, but it closes #{closes[0]}, "
                                           f"which expects {wanted[closes[0]]!r}", number=pr["number"],
                                           milestone=wanted[closes[0]]))
    return changes, settle


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def _read_milestones(repo: str) -> list[dict]:
    pages = json.loads(_gh("api", "--paginate", "--slurp", f"repos/{repo}/milestones?state=all&per_page=100"))
    return [milestone for page in pages for milestone in page]


def _planned_changes(repo: str, stories: dict[str, tuple[dict, dict, dict]], order: dict,
                     existing: list[dict]) -> tuple[list[MilestoneChange], list[str]]:
    issues = json.loads(_gh("issue", "list", "--repo", repo, "--state", "all", "--limit", "5000",
                            "--json", "number,milestone"))
    prs = json.loads(_gh("pr", "list", "--repo", repo, "--state", "open", "--limit", "500",
                         "--json", "number,milestone,closingIssuesReferences"))
    return milestone_changes(order, wanted_milestones(stories, order), existing, issues, prs)


def milestone_drift(repo: str, stories: dict[str, tuple[dict, dict, dict]], order: dict) -> list[str]:
    """Where GitHub's milestones disagree with the order, one line each. Reads only."""
    changes, settle = _planned_changes(repo, stories, order, _read_milestones(repo))
    return [c.says for c in changes] + settle


def apply_milestones(repo: str, stories: dict[str, tuple[dict, dict, dict]], order: dict) -> tuple[list[str], list[str]]:
    """Make GitHub's milestones match the order: ``(what was written, what only a person can settle)``. The milestones
    are written first, since an issue takes a milestone by a number that only exists once it is made. A second run with
    nothing changed writes nothing."""
    existing = _read_milestones(repo)
    changes, settle = _planned_changes(repo, stories, order, existing)
    numbers = {m["title"]: m["number"] for m in existing}
    for c in changes:
        if c.title is None:
            continue
        fields = ("-f", f"title={c.title}", "-f", f"description={c.description}")
        if c.title in numbers:
            _gh("api", "-X", "PATCH", f"repos/{repo}/milestones/{numbers[c.title]}", *fields)
        else:
            numbers[c.title] = json.loads(_gh("api", "-X", "POST", f"repos/{repo}/milestones", *fields))["number"]
    for c in changes:
        if c.number is not None:
            value = "null" if c.milestone is None else str(numbers[c.milestone])  # gh -F sends these as JSON
            _gh("api", "-X", "PATCH", f"repos/{repo}/issues/{c.number}", "-F", f"milestone={value}")
    return [c.says for c in changes], settle


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--drift", metavar="REPO")
    parser.add_argument("--milestones", metavar="REPO")
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
        found = drift(args.drift, stories, order) + milestone_drift(args.drift, stories, order)
        print("\n".join(found) if found else "The backlog, the order and the GitHub issues agree.")
        return 1 if found else 0
    if args.milestones:
        written, settle = apply_milestones(args.milestones, stories, order)
        print("\n".join(f"wrote: {line}" for line in written) if written else "The milestones already match the order.")
        if settle:
            print("For a person to settle:\n  " + "\n  ".join(settle), file=sys.stderr)
        return 1 if settle else 0
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
