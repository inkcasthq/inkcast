#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Third-party container images pinned by digest (E1.F1.8.S2, issue #35).

    scripts/images.py list                 # every third-party image reference, pinned or not
    scripts/images.py resolve              # tag -> digest for each image, as name:tag@sha256:... lines (needs docker)
    scripts/images.py pin [--from FILE]    # rewrite every reference to name:tag@sha256:... (resolving, or from FILE)
    scripts/images.py verify               # CI: each pinned digest still resolves; a tag that moved is a notice

A tag can be re-pushed to point at different bytes; a digest can't. So every image Inkcast runs or builds from is
named by tag (for people) and digest (for the machine). The digest is the multi-arch index, so arm64 laptops and
amd64 clusters get the same pinned build. Dependabot moves the Dockerfile and Compose pins; Kubernetes manifests it
doesn't read, so `verify` in the weekly CI run reports a moved tag there. Inkcast's own image (inkcast, ghcr.io/
inkcasthq/inkcast) is pinned at deploy time by the promote workflow, not here.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = ["Dockerfile", "deploy/**/*.Dockerfile", "deploy/**/*.yaml", "deploy/**/*.yml", ".github/workflows/*.yml"]
# name:tag with at least one registry/namespace part or a library name, then an optional @sha256 digest.
REF = re.compile(r"(?P<name>(?:[a-z0-9][a-z0-9.-]*(?::\d+)?/)?(?:[a-z0-9._-]+/)*[a-z0-9._-]+):(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]*)"
                 r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?")
# Where a reference can appear: FROM, `image:` (manifests, Compose), and a registry-qualified name anywhere (docker run).
CONTEXT = re.compile(r"^\s*FROM\s+(\S+)|^\s*-?\s*image:\s*['\"]?([^\s'\"#]+)"
                     r"|\s((?:quay\.io|docker\.io|gcr\.io|registry\.k8s\.io|mcr\.microsoft\.com)/[^\s'\"]+)")
OURS = ("inkcast", "inkcast-fakes", "ghcr.io/inkcasthq/")


def references() -> list[tuple[pathlib.Path, int, re.Match]]:
    """(file, line number, match) for every third-party image reference."""
    found = []
    for pattern in FILES:
        for path in sorted(ROOT.glob(pattern)):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                ctx = CONTEXT.search(line)
                if not ctx:
                    continue
                text = next(g for g in ctx.groups() if g)
                if "$" in text or text.split(":")[0].split("@")[0] in OURS or text.startswith(OURS[2]):
                    continue
                m = REF.fullmatch(text.split(" ")[0])
                if m:
                    found.append((path, n, m))
    return found


def resolve(name: str, tag: str) -> str:
    r = subprocess.run(["docker", "buildx", "imagetools", "inspect", f"{name}:{tag}", "--format", "{{.Manifest.Digest}}"],
                       capture_output=True, text=True)
    digest = r.stdout.strip()
    if r.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"can't resolve {name}:{tag}: {r.stderr.strip()[:300]}")
    return digest


def unique(refs) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    for _, _, m in refs:
        out.setdefault((m["name"], m["tag"]), set()).add(m["digest"] or "")
    return out


def pin(mapping: dict[tuple[str, str], str]) -> int:
    changed = 0
    for path in {p for p, _, _ in references()}:
        text = path.read_text()
        def repl(m: re.Match) -> str:
            digest = mapping.get((m["name"], m["tag"]))
            return f"{m['name']}:{m['tag']}@{digest}" if digest else m.group(0)
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if CONTEXT.search(line):
                lines[i] = REF.sub(lambda m: repl(m) if (m["name"], m["tag"]) in mapping else m.group(0), line)
        new = "".join(lines)
        if new != text:
            path.write_text(new)
            changed += 1
    return changed


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    refs = references()
    if cmd == "list":
        for path, n, m in refs:
            print(f"{path.relative_to(ROOT)}:{n}  {m.group(0)}{'' if m['digest'] else '   (not pinned)'}")
        return 0
    if cmd == "resolve":
        for name, tag in sorted(unique(refs)):
            print(f"{name}:{tag}@{resolve(name, tag)}")
        return 0
    if cmd == "pin":
        if "--from" in sys.argv:
            lines = pathlib.Path(sys.argv[sys.argv.index("--from") + 1]).read_text().split()
            mapping = {}
            for line in lines:
                m = REF.fullmatch(line)
                if not m or not m["digest"]:
                    raise SystemExit(f"not name:tag@sha256:...: {line}")
                mapping[(m["name"], m["tag"])] = m["digest"]
        else:
            mapping = {(n, t): resolve(n, t) for n, t in unique(refs)}
        print(f"{pin(mapping)} file(s) updated")
        return 0
    if cmd == "verify":
        status, lines = 0, []
        for (name, tag), digests in sorted(unique(refs).items()):
            current = resolve(name, tag)
            lines.append(f"{name}:{tag}@{current}")
            for digest in digests - {""}:
                if digest != current:
                    print(f"::notice title=Image tag moved::{name}:{tag} now points at {current}; pinned {digest}")
                    check = subprocess.run(["docker", "buildx", "imagetools", "inspect", f"{name}@{digest}"],
                                           capture_output=True, text=True)
                    if check.returncode:
                        print(f"::error title=Pinned image missing::{name}@{digest} no longer resolves")
                        status = 1
            if "" in digests:
                print(f"::warning title=Image not pinned::{name}:{tag} has a reference without a digest")
        print("::notice title=Image digests (current)::" + "%0A".join(lines))
        print("\n".join(lines))
        return status
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
