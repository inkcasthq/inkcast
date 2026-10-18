#!/usr/bin/env bash
# Wait for every Deployment in a namespace to roll out. On failure, print a compact diagnosis
# (pod states, recent events, the failing pods' last log lines) so ci-run.sh can surface it.
#   scripts/ci-rollout.sh <namespace> [timeout]
set -uo pipefail
ns="$1"; timeout="${2:-180s}"
failed=0
for d in $(kubectl -n "$ns" get deploy -o name); do
  kubectl -n "$ns" rollout status "$d" --timeout="$timeout" || failed=1
done
if [ "$failed" -ne 0 ]; then
  echo "--- events"; kubectl -n "$ns" get events --sort-by=.lastTimestamp 2>/dev/null | grep -v Normal | tail -n 8
  for p in $(kubectl -n "$ns" get pods -o name); do
    ready=$(kubectl -n "$ns" get "$p" -o jsonpath='{.status.containerStatuses[0].ready}')
    [ "$ready" = "true" ] && continue
    echo "--- $p $(kubectl -n "$ns" get "$p" -o jsonpath='{.status.phase} {.status.containerStatuses[0].state}')"
    kubectl -n "$ns" logs "$p" --tail=6 2>&1 | cut -c1-220
  done
  echo "--- pods"; kubectl -n "$ns" get pods -o wide | cut -c1-120
fi
exit "$failed"
