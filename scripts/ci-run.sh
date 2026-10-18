#!/usr/bin/env bash
# Run a CI command; on failure, surface the end of its output as ONE GitHub annotation
# (GitHub shows at most 10 annotations per step, so the tail is sent as a single multi-line one).
#   scripts/ci-run.sh <label> <command> [args...]
set -uo pipefail
label="$1"; shift
log="$(mktemp)"
"$@" 2>&1 | tee "$log"
status=${PIPESTATUS[0]}
if [ "$status" -ne 0 ]; then
  # Escape for workflow commands: % first, then CR/LF as %0D/%0A.
  tail -n 25 "$log" | sed -e 's/%/%25/g' -e 's/\r/%0D/g' | awk 'BEGIN{ORS="%0A"} {print}' > "$log.msg"
  echo "::error title=${label} failed (exit ${status})::$(cat "$log.msg")"
fi
exit "$status"
