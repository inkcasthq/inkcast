#!/usr/bin/env bash
# Files every docs/issues/NN-*.md as a GitHub issue. Usage: scripts/file-issues.sh owner/repo
set -euo pipefail
repo="${1:?usage: $0 owner/repo}"
cd "$(dirname "$0")/../docs/issues"
for f in [0-9][0-9]-*.md; do
  title="$(sed -n '1s/^# //p' "$f")"
  labels="$(sed -n 's/^labels: //p' "$f")"
  body="$(sed '1,/^labels:/d' "$f")"
  for l in ${labels//,/ }; do gh label create "$l" --repo "$repo" >/dev/null 2>&1 || true; done
  gh issue create --repo "$repo" --title "$title" --label "$labels" --body "$body"
done
