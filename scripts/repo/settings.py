#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Repository settings as code (E1.F1.8.S1, S6; issues #34, #45): the main branch ruleset and the security settings.

    scripts/repo/settings.py check [owner/repo]   # compare the live repository with the files; exit 1 on any drift
    scripts/repo/settings.py apply [owner/repo]   # make the live repository match the files (needs admin)

The files: .github/rulesets/*.json (one ruleset each, the REST payload) and .github/repo-settings.json (merge
methods, branch clean-up, secret scanning with push protection, Dependabot security updates, vulnerability alerts,
private vulnerability reporting). Uses the gh CLI, so it runs with whatever token gh has: the owner's for apply,
GITHUB_TOKEN in the scheduled check (.github/workflows/repo-settings.yml).
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
REPO = "inkcasthq/inkcast"


UNVERIFIED: list[str] = []   # reads this token may not make (admin-only paths): reported, not counted as drift


def gh(method: str, path: str, body: dict | None = None, ok404: bool = False, soft: bool = False) -> tuple[int, object]:
    args = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json", "--include"]
    if body is not None:
        args += ["--input", "-"]
    r = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
    head, _, payload = r.stdout.partition("\r\n\r\n") if "\r\n\r\n" in r.stdout else r.stdout.partition("\n\n")
    status = int(head.split()[1]) if head.startswith("HTTP/") else (0 if r.returncode else 200)
    if r.returncode and soft and method == "GET" and status in (0, 403):
        UNVERIFIED.append(path)
        return -1, None
    if r.returncode and not (ok404 and status == 404):
        sys.exit(f"gh api {method} {path}: HTTP {status} {r.stderr.strip() or payload.strip()[:300]}")
    try:
        return status, json.loads(payload) if payload.strip() else None
    except json.JSONDecodeError:
        return status, payload


def subset(want: object, have: object) -> bool:
    """`want` is contained in `have`: dict keys recursively, lists compared as sets of their items (order-free)."""
    if isinstance(want, dict):
        return isinstance(have, dict) and all(k in have and subset(v, have[k]) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(have, list) and len(want) == len(have) and all(any(subset(w, h) for h in have) for w in want)
    return want == have


def rulesets() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((ROOT / ".github" / "rulesets").glob("*.json"))]


def live_ruleset(repo: str, name: str) -> dict | None:
    _, listed = gh("GET", f"repos/{repo}/rulesets?includes_parents=false")
    found = next((r for r in listed or [] if r.get("name") == name), None)
    return gh("GET", f"repos/{repo}/rulesets/{found['id']}")[1] if found else None


def check(repo: str) -> list[str]:
    drift = []
    for want in rulesets():
        have = live_ruleset(repo, want["name"])
        if not have:
            drift.append(f"ruleset {want['name']!r} is missing")
            continue
        for key in ("target", "enforcement", "bypass_actors", "conditions"):
            if not subset(want[key], have.get(key)):
                drift.append(f"ruleset {want['name']!r}: {key} is {json.dumps(have.get(key))}, want {json.dumps(want[key])}")
        live_rules = {r["type"]: r for r in have.get("rules", [])}
        for rule in want["rules"]:
            if rule["type"] not in live_rules:
                drift.append(f"ruleset {want['name']!r}: rule {rule['type']} is missing")
            elif not subset(rule.get("parameters", {}), live_rules[rule["type"]].get("parameters", {})):
                drift.append(f"ruleset {want['name']!r}: rule {rule['type']} differs: live "
                             f"{json.dumps(live_rules[rule['type']].get('parameters'))}")
        extra = set(live_rules) - {r["type"] for r in want["rules"]}
        if extra:
            drift.append(f"ruleset {want['name']!r}: live rules not in the file: {sorted(extra)}")

    settings = json.loads((ROOT / ".github" / "repo-settings.json").read_text())
    _, live = gh("GET", f"repos/{repo}")
    for key, value in settings["repository"].items():
        if live.get(key) != value:
            drift.append(f"repository {key} is {live.get(key)!r}, want {value!r}")
    analysis = live.get("security_and_analysis") or {}
    for key, value in settings["security_and_analysis"].items():
        if (analysis.get(key) or {}).get("status") != value:
            drift.append(f"security_and_analysis {key} is {(analysis.get(key) or {}).get('status')!r}, want {value!r}")
    status, _ = gh("GET", f"repos/{repo}/vulnerability-alerts", ok404=True, soft=True)
    if status != -1 and (status == 204) != settings["vulnerability_alerts"]:
        drift.append(f"vulnerability alerts are {'on' if status == 204 else 'off'}")
    status, pvr = gh("GET", f"repos/{repo}/private-vulnerability-reporting", soft=True)
    if status != -1 and bool((pvr or {}).get("enabled")) != settings["private_vulnerability_reporting"]:
        drift.append(f"private vulnerability reporting is {'on' if (pvr or {}).get('enabled') else 'off'}")
    return drift


def apply(repo: str) -> None:
    settings = json.loads((ROOT / ".github" / "repo-settings.json").read_text())
    gh("PATCH", f"repos/{repo}", {**settings["repository"], "security_and_analysis": {
        k: {"status": v} for k, v in settings["security_and_analysis"].items()}})
    gh("PUT" if settings["vulnerability_alerts"] else "DELETE", f"repos/{repo}/vulnerability-alerts")
    gh("PUT" if settings["private_vulnerability_reporting"] else "DELETE", f"repos/{repo}/private-vulnerability-reporting")
    for want in rulesets():
        have = live_ruleset(repo, want["name"])
        if have:
            gh("PUT", f"repos/{repo}/rulesets/{have['id']}", want)
            print(f"ruleset {want['name']!r} updated (id {have['id']})")
        else:
            _, made = gh("POST", f"repos/{repo}/rulesets", want)
            print(f"ruleset {want['name']!r} created (id {made['id']})")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("check", "apply"):
        print(__doc__)
        return 2
    repo = sys.argv[2] if len(sys.argv) > 2 else REPO
    if sys.argv[1] == "apply":
        apply(repo)
    drift = check(repo)
    for line in drift:
        print(f"DRIFT: {line}")
    for path in UNVERIFIED:
        print(f"NOT VERIFIED (this token can't read it): {path}")
    print("repository settings match the files" if not drift else f"{len(drift)} difference(s) from the files")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
