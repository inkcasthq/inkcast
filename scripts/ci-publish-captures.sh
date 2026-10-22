#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# Push CI captures to the `ci-captures` branch, so screenshots can be viewed on GitHub (or with git) without
# downloading artifacts. Run by publish.yml on main only.
#
#   scripts/ci-publish-captures.sh <dir>     # <dir>/<artifact-name>-<sha>/... as `gh run download` leaves it
#
# Each artifact replaces <name>/latest/ on the branch (the branch history keeps the older ones), and README.md
# shows every latest PNG; a folder holding the end-to-end walkthrough (walkthrough.html, walkthrough.pdf; issue #57)
# links the document first. Nothing to publish is not an error. The branch is an orphan: no source code on it.
set -euo pipefail
src="$1"
if [ ! -d "$src" ] || [ -z "$(ls -A "$src" 2>/dev/null)" ]; then
  echo "No captures to publish."; exit 0
fi
sha="${GITHUB_SHA:-$(git rev-parse HEAD)}"
work="$(mktemp -d)"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
if git ls-remote --exit-code --heads origin ci-captures >/dev/null 2>&1; then
  git fetch --quiet --depth 1 origin ci-captures
  git worktree add --quiet --detach "$work" FETCH_HEAD
else
  git worktree add --quiet --detach "$work"
  (cd "$work" && git checkout --quiet --orphan ci-captures-new && git rm -rfq --ignore-unmatch . && git clean -fdxq)
fi
for dir in "$src"/*/; do
  name="$(basename "$dir")"
  name="${name%-*}"                                   # dashboards-kind-<sha> -> dashboards-kind
  rm -rf "${work:?}/$name/latest"
  mkdir -p "$work/$name/latest"
  cp -R "$dir". "$work/$name/latest/"
done
{
  echo "# CI captures"
  echo
  echo "Published by \`publish.yml\` from \`main\` at ${sha:0:12}. Each folder's \`latest/\` is the newest run;"
  echo "the branch history keeps the older ones. Light theme only."
  for d in "$work"/*/latest; do
    name="$(basename "$(dirname "$d")")"
    echo; echo "## $name"; echo
    if [ -f "$d/walkthrough.html" ]; then
      echo "The end-to-end walkthrough: [walkthrough.html]($name/latest/walkthrough.html) · [walkthrough.pdf]($name/latest/walkthrough.pdf)"
      echo "(data: [results.json]($name/latest/results.json), [integrity.jsonl]($name/latest/integrity.jsonl), [env.json]($name/latest/env.json))."
      echo
    fi
    [ -f "$d/summary.md" ] && echo "[Summary]($name/latest/summary.md)" && echo
    for png in "$d"/*.png; do [ -f "$png" ] && echo "![$name]($name/latest/$(basename "$png"))"; done
  done
} > "$work/README.md"
cd "$work"
git add -A
if git diff --cached --quiet; then echo "Captures unchanged."; exit 0; fi
git commit --quiet -m "CI captures for ${sha:0:12}"
git push --quiet origin HEAD:refs/heads/ci-captures
echo "Published to ci-captures: $(ls -d */ | tr '\n' ' ')"
