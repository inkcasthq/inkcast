#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# CodeQL locally: the analysis CI runs (.github/workflows/codeql.yml), before you push (issue #28).
#
#   scripts/dev/codeql.sh              # Python and JavaScript, the CI config; prints every result with its path
#   LANGS=python scripts/dev/codeql.sh # one language (python, javascript-typescript)
#
# Same bundle as CI (the one the pinned github/codeql-action uses; tests/test_architecture.py keeps them in step),
# same config (.github/codeql/codeql-config.yml: security-extended, paths-ignore), same query suites. Each result is
# printed with its code flow, source to sink, which the Security tab shows but CI logs don't. Exits 1 at CI's
# failure threshold: a security severity of high or above, or an error-level result.
#
# The bundle (about 700 MB) is downloaded once into ~/.cache/inkcast/codeql and checked against the SHA-256 below.
# Databases and SARIF go to .codeql/ (git-ignored). Linux x86-64 and macOS.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Bump together with the github/codeql-action pin in codeql.yml: the bundle that version uses is
# "bundleVersion" in the action's src/defaults.json at the pinned commit.
CODEQL_ACTION_SHA=2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2   # v4.38.2
CODEQL_BUNDLE=codeql-bundle-v2.27.1
SHA256_linux64=1d380f79896ededc654c7b21fafb3360136f1aeb678ad4df4df9af3910c6b815
SHA256_osx64=bca7fe16689986793b8bbc75cf1e0809497b9baed7f10cde42f403eca1390c0a

CONFIG="$ROOT/.github/codeql/codeql-config.yml"
LANGS="${LANGS:-python,javascript-typescript}"
OUT="$ROOT/.codeql"
CACHE="${CODEQL_CACHE:-$HOME/.cache/inkcast/codeql}"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) platform=linux64 ;;
  Darwin-*) platform=osx64 ;;
  *) echo "CodeQL bundles exist for Linux x86-64 and macOS; this is $(uname -s)-$(uname -m)" >&2; exit 2 ;;
esac
want="SHA256_$platform"; want="${!want}"

codeql="$CACHE/$CODEQL_BUNDLE/codeql/codeql"
if [ ! -x "$codeql" ]; then
  mkdir -p "$CACHE"
  archive="$CACHE/$CODEQL_BUNDLE-$platform.tar.gz"
  if [ ! -s "$archive" ]; then
    echo "== Downloading $CODEQL_BUNDLE ($platform, about 700 MB, once)"
    curl -fSL --retry 3 -o "$archive.part" \
      "https://github.com/github/codeql-action/releases/download/$CODEQL_BUNDLE/codeql-bundle-$platform.tar.gz"
    mv "$archive.part" "$archive"
  fi
  got="$( (command -v sha256sum >/dev/null && sha256sum "$archive" || shasum -a 256 "$archive") | cut -d' ' -f1)"
  if [ "$got" != "$want" ]; then
    rm -f "$archive"
    echo "the downloaded bundle's SHA-256 is $got, not the pinned $want: deleted, not used" >&2
    exit 1
  fi
  mkdir -p "$CACHE/$CODEQL_BUNDLE"
  tar -xzf "$archive" -C "$CACHE/$CODEQL_BUNDLE"
  rm -f "$archive"
fi
echo "== $("$codeql" version --format=terse) ($CODEQL_BUNDLE), config $(basename "$CONFIG"), languages $LANGS"

rm -rf "$OUT/db"
mkdir -p "$OUT"
"$codeql" database create "$OUT/db" --db-cluster --language="$LANGS" --source-root="$ROOT" \
  --codescanning-config="$CONFIG" --overwrite --quiet

status=0
IFS=, read -ra langs <<< "$LANGS"
for lang in "${langs[@]}"; do
  pack="$lang"; [ "$lang" = javascript-typescript ] && pack=javascript
  sarif="$OUT/$lang.sarif"
  echo "== Analysing $lang (codeql/$pack-queries, $pack-security-extended)"
  "$codeql" database analyze "$OUT/db/$pack" "codeql/$pack-queries:codeql-suites/$pack-security-extended.qls" \
    --format=sarif-latest --output="$sarif" --sarif-add-snippets --quiet
  python3 "$ROOT/scripts/dev/sarif_report.py" "$sarif" "$ROOT" || status=1
done
[ "$status" = 0 ] && echo "CodeQL: nothing at CI's failure threshold" || echo "CodeQL: results at CI's failure threshold (above)"
exit "$status"
