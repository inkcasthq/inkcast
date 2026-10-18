#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Print a CodeQL SARIF file the way a reviewer needs it (scripts/dev/codeql.sh, issue #28).

    python3 sarif_report.py results.sarif [repo-root]

One block per result, most severe first: the rule, its security severity, where it is, the message, and the code
flow from source to sink, each step with its line of code. Exits 1 if any result is at the threshold that fails
the CodeQL check on a pull request: a security severity of 7.0 (high) or more, or level "error".
"""

from __future__ import annotations

import json
import pathlib
import sys

THRESHOLD = 7.0


def where(location: dict) -> tuple[str, int]:
    physical = location.get("physicalLocation", {})
    return physical.get("artifactLocation", {}).get("uri", "?"), physical.get("region", {}).get("startLine", 0)


def line_of(root: pathlib.Path, uri: str, line: int) -> str:
    try:
        return (root / uri).read_text(encoding="utf-8").splitlines()[line - 1].strip()
    except (OSError, IndexError, UnicodeDecodeError):
        return ""


def main() -> int:
    sarif = json.loads(pathlib.Path(sys.argv[1]).read_text())
    root = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else ".")
    failing, results = 0, []
    for run in sarif.get("runs", []):
        rules = {}
        for component in [run["tool"]["driver"], *run["tool"].get("extensions", [])]:
            for rule in component.get("rules", []):
                rules[rule["id"]] = rule
        for result in run.get("results", []):
            rule = rules.get(result.get("ruleId", ""), {})
            props = rule.get("properties", {})
            severity = float(props.get("security-severity", 0) or 0)
            level = result.get("level") or rule.get("defaultConfiguration", {}).get("level", "warning")
            results.append((severity, level, result, rule))
    results.sort(key=lambda r: (-r[0], r[1] != "error"))
    for severity, level, result, rule in results:
        fails = severity >= THRESHOLD or level == "error"
        failing += fails
        uri, line = where(result["locations"][0])
        label = (f"security {severity:.1f}" if severity else level) + (" FAILS THE CHECK" if fails else "")
        print(f"\n[{label}] {result.get('ruleId')}: {rule.get('shortDescription', {}).get('text', '')}")
        print(f"  {uri}:{line}  {line_of(root, uri, line)}")
        print(f"  {result['message']['text']}")
        for flow in result.get("codeFlows", [])[:1]:
            for thread in flow.get("threadFlows", [])[:1]:
                steps = thread.get("locations", [])
                print(f"  path ({len(steps)} steps, source first):")
                for i, step in enumerate(steps, 1):
                    loc = step.get("location", {})
                    s_uri, s_line = where(loc)
                    note = loc.get("message", {}).get("text", "")
                    print(f"    {i:>2}. {s_uri}:{s_line}  {line_of(root, s_uri, s_line)}" + (f"   <- {note}" if note else ""))
    name = pathlib.Path(sys.argv[1]).stem
    print(f"\n{name}: {len(results)} result(s), {failing} at the check's failure threshold")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
