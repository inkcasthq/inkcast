#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The web workspace's npm lock (E4.F4.1.S5, issue #348), resolved by .github/workflows/web-lock-refresh.yml.
#
#   scripts/web-lock.sh [moved-file]   # rewrite web/package-lock.json; list what moved on stdout (and in moved-file)
#
# It resolves from scratch: no node_modules and no old lock. npm before 11.3.0 drops other platforms' optional
# native packages from a lock written where node_modules exists (npm/cli#4828, fixed by npm/cli#8184), and a Linux
# `npm ci` then can't find them (npm/cli#8320). Every entry gets npm's sha512 integrity and no install script runs.
# It runs with the Node that web/package.json's engines.node pins, so the npm that Node ships writes the lock.
set -euo pipefail
WEB="$(cd "$(dirname "$0")/../web" && pwd)"
moved="${1:-/dev/null}"
cd "$WEB"

stale="$(find . -name node_modules -type d -prune -print)"
if [ -n "$stale" ]; then
  echo "node_modules found (${stale//$'\n'/, }): remove it, or run this in a clean checkout (npm/cli#4828)" >&2
  exit 1
fi
want="v$(node -p 'require("./package.json").engines.node')"
if [ "$want" != "$(node --version)" ]; then
  echo "web/package.json pins Node $want; this is $(node --version)" >&2
  exit 1
fi

old="$(mktemp)"; trap 'rm -f "$old"' EXIT
if [ -f package-lock.json ]; then mv package-lock.json "$old"; else echo '{}' > "$old"; fi
npm install --package-lock-only --ignore-scripts --no-audit --no-fund --no-update-notifier
echo "web/package-lock.json written by Node $(node --version), npm $(npm --version)"

# What moved, by install path: "name old -> new", "added name version" or "removed name version".
node - "$old" package-lock.json <<'JS' | tee "$moved"
const fs = require("fs");
const [before, after] = process.argv.slice(2).map((f) => JSON.parse(fs.readFileSync(f, "utf8")).packages || {});
const lines = [];
for (const key of [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()) {
  if (!key.includes("node_modules/")) continue;   // the root and the workspaces, which carry no version pin
  const name = key.replace(/^node_modules\//, "");
  const [was, now] = [before[key]?.version, after[key]?.version];
  if (was === now) continue;
  lines.push(was === undefined ? `added ${name} ${now}` : now === undefined ? `removed ${name} ${was}` : `${name} ${was} -> ${now}`);
}
console.log(lines.length ? lines.join("\n") : "no version moved");
JS
