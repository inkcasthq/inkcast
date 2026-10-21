#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# Hash-locked Python dependencies for the image (E1.F1.8.S3, issue #36).
#
#   scripts/lock.sh            # (re)write the locks, keeping every pin that still satisfies pyproject.toml
#   scripts/lock.sh --upgrade  # move every pin to the newest allowed version (the weekly lock-refresh workflow)
#   scripts/lock.sh --check    # CI: exit 1 if the locks no longer satisfy pyproject.toml or build-requirements.in
#
# requirements.lock holds the runtime dependencies of the wheel, transitive ones included; build.lock holds the
# build backend (hatchling and its dependencies). Every pin carries the sha256 of every file PyPI publishes for that
# version, so the image's `pip install --require-hashes` refuses anything else, on any architecture. Resolved for
# the image's Python (PY, default 3.14) with uv (pinned in the workflow), which needs to reach PyPI.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-3.14}"
mode="${1:-}"
cd "$ROOT"

compile() {  # input output [extra uv flags]
  local input="$1" output="$2"; shift 2
  uv pip compile "$input" --output-file "$output" --generate-hashes --universal --python-version "$PY" \
    --custom-compile-command "scripts/lock.sh" --quiet "$@"
}

case "$mode" in
  ""|--upgrade)
    flags=(); [ "$mode" = --upgrade ] && flags=(--upgrade)
    compile pyproject.toml requirements.lock "${flags[@]}"
    compile build-requirements.in build.lock "${flags[@]}"
    echo "locks written: requirements.lock, build.lock (Python $PY)"
    ;;
  --check)
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    status=0
    for pair in "pyproject.toml requirements.lock" "build-requirements.in build.lock"; do
      set -- $pair
      [ -f "$2" ] || { echo "$2 is missing (run scripts/lock.sh)" >&2; status=1; continue; }
      cp "$2" "$tmp/$2"                      # uv keeps the pins it finds in the output file
      compile "$1" "$tmp/$2"
      if ! diff -u "$2" "$tmp/$2"; then
        echo "$2 no longer matches $1 (run scripts/lock.sh and commit the result)" >&2
        status=1
      fi
    done
    exit "$status"
    ;;
  *) sed -n '5,9p' "$0"; exit 2 ;;
esac
