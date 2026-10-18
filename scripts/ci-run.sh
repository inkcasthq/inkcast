#!/usr/bin/env bash
# Run a CI command; on failure, surface the last lines of its output as GitHub annotations,
# so the cause is visible on the run summary without opening (or downloading) the full log.
#   scripts/ci-run.sh <label> <command> [args...]
set -uo pipefail
label="$1"; shift
log="$(mktemp)"
"$@" 2>&1 | tee "$log"
status=${PIPESTATUS[0]}
if [ "$status" -ne 0 ]; then
  echo "::error title=${label} failed (exit ${status})::see the last lines below"
  tail -n 40 "$log" | sed -e 's/%/%25/g' -e 's/\r//g' | while IFS= read -r line; do
    [ -n "$line" ] && echo "::error title=${label}::${line}"
  done
fi
exit "$status"
