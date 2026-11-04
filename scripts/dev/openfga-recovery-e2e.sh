#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The longer proofs of OpenFGA's schema migration as an init container (E3.F3.9.S8, issue #337; the review of PR
# #374), on kind, on request: too long for every CI run, so they run once per change of the migration's manifests
# or script and their output is committed to docs/epics/E3-tenancy-authorization/evidence/openfga-recovery-e2e.txt.
# OpenFGA v1.22 takes no lock around its migrations, so components/openfga/migrate.sh holds a session-level advisory
# lock across each run and repairs an interrupted 006 first (deployment.yaml cites the sources). Each proof fails
# the script:
#
#   race     two pods migrate one fresh database at the same time: a fresh namespace, the Deployment scaled to two
#            before the Cluster is ready, both init containers reach the database together. Both pods end Ready,
#            goose's table holds one row per version with the newest applied row the highest version (what
#            goose.GetDBVersion reads: ORDER BY id DESC), idx_user_lookup is valid, and the table is printed for
#            the record. Then the Deployment goes back to one replica, the count the component ships.
#   overlap  two runners that really overlap, serialised by the lock: a fresh database, the lock taken from
#            outside (psql on the primary, as a superuser, for HOLD_SECONDS), then two pods started together. Both
#            init containers log the holder and wait; when the holder's session ends one of them migrates the
#            empty database to revision 6 and the other, which takes the lock only after that one has released
#            it (the timestamps in the logs are compared), finds revision 6 and applies nothing. Then the same
#            invariants as `race`: no duplicate goose row, the newest applied row the highest version. A delayed
#            003 replayed after 006 (the P1 sequence of the review) needs a runner inside another's run, which the
#            lock makes impossible by construction; this proof shows the lock holds two real runners apart.
#   repair   an INVALID idx_user_lookup with version 6 recorded (what an interrupted 006 and a later run's IF NOT
#            EXISTS leave: the index skipped for good) is repaired before the migration: the index is made
#            invalid the way PostgreSQL leaves one, by a concurrent build that fails (create_index.sgml), under
#            006's name, with version 6 still recorded; the pod is restarted; its init container drops the index,
#            removes the version-6 rows, and `openfga migrate` finds revision 5 and applies 006 again: the index
#            is valid afterwards, the table consistent. The one throwaway tuple the failing build needs is removed
#            before the pod restarts.
#   outage   the database away for longer than the old Job's 30-minute deadline (OUTAGE_SECONDS, default 1860) while
#            a rollout is pending: the Cluster hibernated (CloudNativePG's cnpg.io/hibernation annotation: the pods
#            removed, the volume kept; docs/src/declarative_hibernation.md at v1.30.1), the Deployment restarted so
#            the new pod's init container migrate fails against no database and is retried by the kubelet; then the
#            Cluster rehydrated, and the pod becomes Ready with no apply and no delete: the time from the database's
#            return to Ready is printed, with the init container's restarts and its log.
#   all      every proof, in that order (the default).
#
#   scripts/dev/openfga-recovery-e2e.sh [race|overlap|repair|outage|all]   # after scripts/dev/openfga-e2e.sh (it
#                                                       # installs the CloudNativePG operator), on a kind cluster
#                                                       # with the inkcast:ci image loaded, as in the kind CI job
#   OUTAGE_SECONDS=120 scripts/dev/openfga-recovery-e2e.sh outage   # a shorter outage, to try the script itself
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
k="${KUBECTL:-kubectl}"
ns=inkcast-openfga
mode="${1:-all}"
OUTAGE_SECONDS="${OUTAGE_SECONDS:-1860}"
HOLD_SECONDS="${HOLD_SECONDS:-45}"
case "$mode" in race|overlap|repair|outage|all) ;; *) echo "usage: $0 [race|overlap|repair|outage|all]" >&2; exit 2 ;; esac
# The lock key migrate.sh takes, read from the script itself so the proof and the pod agree.
# shellcheck disable=SC2016  # the pattern's `\$` is a literal dollar: the script's own text, not an expansion
LOCK_KEY="$(sed -n 's/^lock_key="\${LOCK_KEY:-\([0-9]*\)}"$/\1/p' "$ROOT/deploy/kustomize/components/openfga/migrate.sh")"
[ -n "$LOCK_KEY" ] || { echo "migrate.sh does not declare its lock key" >&2; exit 2; }

has() {  # has TEXT NEEDLE: no pipe into grep -q (under pipefail that fails at random)
  case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac
}
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
openfga_pods() {  # the running OpenFGA pods, oldest first
  $k -n "$ns" get pods -l app.kubernetes.io/name=openfga,app.kubernetes.io/component=authz --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[*].metadata.name}'
}
all_openfga_pods() {
  $k -n "$ns" get pods -l app.kubernetes.io/name=openfga,app.kubernetes.io/component=authz \
    --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[*].metadata.name}'
}
migrate_restarts() { $k -n "$ns" get pod "$1" -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].restartCount}'; }
migrate_state() { $k -n "$ns" get pod "$1" -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].state}'; }
migrate_log() { $k -n "$ns" logs "$1" -c migrate 2>/dev/null || true; }
revision_found() {  # revision_found POD: the schema revision the init container migrate found, from its log
  migrate_log "$1" | python3 -c '
import json, sys
found = [json.loads(line).get("current version") for line in sys.stdin if line.startswith("{")]
found = [v for v in found if v is not None]
print(found[-1] if found else "none")'
}
primary() { $k -n "$ns" get pods -l cnpg.io/cluster=openfga-db,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}'; }
sql() {  # sql QUERY: on the Cluster's primary, as the operator's own `kubectl cnpg psql` does
  $k -n "$ns" exec "$(primary)" -c postgres -- psql -U postgres -d openfga -X -At -c "$1"
}
conditions() { $k -n "$ns" get deploy openfga -o jsonpath='{range .status.conditions[*]}{.type}={.status} ({.reason}) {.lastTransitionTime}{"\n"}{end}'; }
pod_ready() {  # pod_ready POD SECONDS: wait for the pod's own Ready condition
  for _ in $(seq 1 "$(( $2 / 5 ))"); do
    [ "$($k -n "$ns" get pod "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" = True ] && return 0
    sleep 5
  done
  echo "$1 is not Ready after $2 s" >&2; $k -n "$ns" describe pod "$1" | tail -20 >&2; return 1
}
progressing_again() {  # the controller sees the Ready pod and replaces ProgressDeadlineExceeded with NewReplicaSetAvailable
  for _ in $(seq 1 60); do
    [ "$($k -n "$ns" get deploy openfga -o jsonpath='{.status.conditions[?(@.type=="Progressing")].reason}')" = NewReplicaSetAvailable ] && return 0
    sleep 5
  done
  echo "the Deployment did not report NewReplicaSetAvailable within 300 s" >&2; conditions >&2; return 1
}
fresh_namespace() {  # a fresh database: the namespace goes with the Cluster and its volume; the apply makes them again
  $k delete namespace "$ns" --ignore-not-found --timeout=300s
  $k apply -k "$ROOT/deploy/kustomize/overlays/ci-openfga" >/dev/null
}
goose_table() {  # the table as goose reads it, and the invariants a serialised history keeps
  echo "-- goose_db_version on the primary (id, version_id, is_applied, tstamp):"
  sql "SELECT id, version_id, is_applied, tstamp FROM goose_db_version ORDER BY id"
  local rows distinct newest latest valid reverse
  rows="$(sql "SELECT count(*) FROM goose_db_version")"
  distinct="$(sql "SELECT count(DISTINCT version_id) FROM goose_db_version")"
  newest="$(sql "SELECT version_id FROM goose_db_version WHERE is_applied ORDER BY id DESC LIMIT 1")"   # goose.GetDBVersion
  latest="$(sql "SELECT max(version_id) FROM goose_db_version")"
  valid="$(sql "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')")"
  reverse="$(sql "SELECT to_regclass('idx_reverse_lookup_user') IS NOT NULL")"
  echo "$rows rows, $distinct distinct versions, newest applied row at version $newest, highest $latest; idx_user_lookup valid: $valid; idx_reverse_lookup_user present: $reverse"
  [ "$rows" = "$distinct" ] || { echo "duplicate goose rows: $rows rows for $distinct versions" >&2; exit 1; }
  [ "$newest" = "$latest" ] && [ "$latest" -ge 6 ] || { echo "goose would report revision $newest, not $latest" >&2; exit 1; }
  [ "$valid" = t ] && [ "$reverse" = f ] || { echo "006 is not in effect: idx_user_lookup valid=$valid, idx_reverse_lookup_user present=$reverse" >&2; exit 1; }
}
all_ready() {  # all_ready POD...: each pod Ready
  local pod ready
  for pod in "$@"; do
    ready="$($k -n "$ns" get pod "$pod" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')"
    [ "$ready" = True ] || { echo "$pod is not Ready" >&2; exit 1; }
  done
}

race() {
  echo "== race: two pods migrate one fresh database at the same time ($(now))"
  # Two replicas before the Cluster is ready, so both init containers wait on the same first connection and start
  # together; migrate.sh's lock lets one of them migrate while the other waits, or finds the revision current.
  fresh_namespace
  $k -n "$ns" scale deploy/openfga --replicas=2
  $k -n "$ns" rollout status deploy/openfga --timeout=600s
  read -r -a pods <<< "$(openfga_pods)"
  [ "${#pods[@]}" -eq 2 ] || { echo "expected two running OpenFGA pods, found: ${pods[*]}" >&2; exit 1; }
  for pod in "${pods[@]}"; do
    echo "-- $pod: started $($k -n "$ns" get pod "$pod" -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].state.terminated.startedAt}'), finished $($k -n "$ns" get pod "$pod" -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].state.terminated.finishedAt}'), restarts $(migrate_restarts "$pod"), found revision $(revision_found "$pod")"
    migrate_log "$pod" | cut -c1-200
  done
  goose_table
  all_ready "${pods[@]}"
  echo "both pods Ready on revision 6; back to one replica"
  $k -n "$ns" scale deploy/openfga --replicas=1
  $k -n "$ns" rollout status deploy/openfga --timeout=300s
  echo "PASS race: two init containers started on the same fresh database left one row per version, and both pods serve on one schema revision (overlap shows them contend)"
}

lock_time() {  # lock_time POD taken|released: the clock_timestamp migrate.sh printed, as epoch seconds with fraction
  migrate_log "$1" | python3 -c '
import re, sys
from datetime import datetime
what = sys.argv[1]
found = re.findall(r"migration lock " + what + r" by pid \d+ at (\S+ \S+)", sys.stdin.read())
stamp = re.sub(r"([+-]\d\d)$", r"\1:00", found[-1]) if found else ""   # +00 as psql prints it: +00:00 for every python3
print(datetime.fromisoformat(stamp).timestamp() if found else "none")' "$2"
}

overlap() {
  echo "== overlap: two runners held behind the lock taken from outside, then serialised ($(now))"
  # A fresh database with no pod yet, the lock taken on it from outside for HOLD_SECONDS (a psql session on the
  # primary, as a superuser: pg_advisory_lock then pg_sleep, the session released when the sleep ends), then two
  # pods at once: both init containers find the database, name the holder and wait.
  fresh_namespace
  $k -n "$ns" scale deploy/openfga --replicas=0 >/dev/null
  $k -n "$ns" wait --for=condition=Ready cluster/openfga-db --timeout=600s >/dev/null
  $k -n "$ns" exec "$(primary)" -c postgres -- psql -U postgres -d openfga -X -At \
    -c "SELECT pg_advisory_lock($LOCK_KEY), pg_sleep($HOLD_SECONDS)" >/dev/null 2>&1 &
  holder=$!
  for _ in $(seq 1 30); do
    [ "$(sql "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND ((classid::bigint << 32) | objid::bigint) = $LOCK_KEY")" = 1 ] && break
    sleep 1
  done
  holder_pid="$(sql "SELECT l.pid FROM pg_locks l WHERE l.locktype = 'advisory' AND l.granted AND ((l.classid::bigint << 32) | l.objid::bigint) = $LOCK_KEY")"
  [ -n "$holder_pid" ] || { echo "the lock was not taken from outside" >&2; exit 1; }
  echo "-- the migration lock $LOCK_KEY held from outside by pid $holder_pid for $HOLD_SECONDS s; two pods started at $(now)"
  $k -n "$ns" scale deploy/openfga --replicas=2 >/dev/null
  waiting=""
  for _ in $(seq 1 60); do
    read -r -a pods <<< "$(all_openfga_pods)"
    waiting=0
    for pod in "${pods[@]:-}"; do
      [ -n "$pod" ] && has "$(migrate_log "$pod")" "waiting for the migration lock, held by pid $holder_pid" && waiting=$(( waiting + 1 ))
    done
    [ "$waiting" = 2 ] && break
    sleep 2
  done
  [ "$waiting" = 2 ] || { echo "expected both init containers waiting on pid $holder_pid, found $waiting" >&2; for pod in "${pods[@]:-}"; do migrate_log "$pod" | cut -c1-200; done; exit 1; }
  echo "both init containers wait on the holder (polling pg_try_advisory_lock, idle in between: $(sql "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'openfga-migrate'") sessions, $(sql "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'openfga-migrate' AND wait_event_type = 'Lock'") blocked on a lock), goose table present: $(sql "SELECT to_regclass('goose_db_version') IS NOT NULL")"
  wait "$holder" || true
  echo "-- the holder's session ended at $(now)"
  $k -n "$ns" rollout status deploy/openfga --timeout=600s
  read -r -a pods <<< "$(openfga_pods)"
  [ "${#pods[@]}" -eq 2 ] || { echo "expected two running OpenFGA pods, found: ${pods[*]}" >&2; exit 1; }
  for pod in "${pods[@]}"; do
    echo "-- $pod: found revision $(revision_found "$pod")"
    migrate_log "$pod" | cut -c1-200
  done
  # The order: the pod that took the lock first released it before the other took it.
  first="${pods[0]}"; second="${pods[1]}"
  if [ "$(lock_time "$first" taken)" != none ] && [ "$(lock_time "$second" taken)" != none ] \
     && python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) > float(sys.argv[2]) else 1)' "$(lock_time "$first" taken)" "$(lock_time "$second" taken)"; then
    first="${pods[1]}"; second="${pods[0]}"
  fi
  python3 -c '
import sys
released, taken = float(sys.argv[1]), float(sys.argv[2])
gap = taken - released
print(f"serialised: {sys.argv[3]} released the lock {gap * 1000:.0f} ms before {sys.argv[4]} took it")
sys.exit(0 if gap >= 0 else 1)' "$(lock_time "$first" released)" "$(lock_time "$second" taken)" "$first" "$second" \
    || { echo "the second runner took the lock before the first released it" >&2; exit 1; }
  [ "$(revision_found "$first")" = 0 ] && [ "$(revision_found "$second")" = 6 ] \
    || { echo "expected the first runner to find revision 0 and the second 6, found $(revision_found "$first") and $(revision_found "$second")" >&2; exit 1; }
  goose_table
  all_ready "${pods[@]}"
  $k -n "$ns" scale deploy/openfga --replicas=1
  $k -n "$ns" rollout status deploy/openfga --timeout=300s
  echo "PASS overlap: two runners held behind the lock were serialised, the first migrated the empty database and the second found revision 6, one row per version"
}

repair() {
  echo "== repair: an INVALID idx_user_lookup with version 6 recorded is repaired before the migration ($(now))"
  $k -n "$ns" rollout status deploy/openfga --timeout=600s
  before="$(openfga_pods)"
  # The state an interrupted 006 and a later run leave, made the way PostgreSQL leaves it: a concurrent build that
  # fails leaves its index INVALID (create_index.sgml, "Building Indexes Concurrently"). The build needs one row to
  # evaluate its failing expression on; that throwaway tuple goes before the pod restarts. Version 6 stays recorded.
  sql "INSERT INTO tuple (store, object_type, object_id, relation, _user, user_type, ulid, inserted_at) VALUES ('e2e-repair', 'document', 'repair', 'viewer', 'user:repair', 'user', '01E2EREPAIR0000000000000000', now())" >/dev/null
  sql "DROP INDEX idx_user_lookup"
  failed="$(sql "CREATE INDEX CONCURRENTLY idx_user_lookup ON tuple ((1 / (0 * length(store))))" 2>&1 || true)"
  has "$failed" "division by zero" || { echo "the concurrent build did not fail as arranged: $failed" >&2; exit 1; }
  sql "DELETE FROM tuple WHERE store = 'e2e-repair'" >/dev/null
  echo "-- before: idx_user_lookup valid: $(sql "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')"), version-6 rows: $(sql "SELECT count(*) FROM goose_db_version WHERE version_id = 6"), tuples: $(sql "SELECT count(*) FROM tuple")"
  [ "$(sql "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')")" = f ] || { echo "the index is not INVALID" >&2; exit 1; }
  $k -n "$ns" rollout restart deploy/openfga >/dev/null
  $k -n "$ns" rollout status deploy/openfga --timeout=600s
  pod="$(openfga_pods)"
  has "$before" "$pod" && { echo "the restart made no new pod" >&2; exit 1; }
  log="$(migrate_log "$pod")"; cut -c1-200 <<< "$log"
  has "$log" "repair: idx_user_lookup is INVALID" || { echo "migrate.sh did not drop the invalid index" >&2; exit 1; }
  has "$log" "repair: version 6 is recorded without a valid idx_user_lookup" || { echo "migrate.sh did not remove the version-6 rows" >&2; exit 1; }
  [ "$(revision_found "$pod")" = 5 ] || { echo "openfga migrate should have found revision 5 after the repair, found $(revision_found "$pod")" >&2; exit 1; }
  goose_table
  [ "$(sql "SELECT count(*) FROM tuple")" = 0 ] || { echo "a tuple was left behind" >&2; exit 1; }
  echo "PASS repair: the invalid idx_user_lookup was dropped with its version-6 rows, 006 was applied again, and the index is valid"
}

outage() {
  echo "== outage: the database away ${OUTAGE_SECONDS} s while a rollout is pending ($(now))"
  $k -n "$ns" rollout status deploy/openfga --timeout=600s
  before="$(openfga_pods)"
  echo "-- hibernating the Cluster (the pods removed, the volume kept)"
  $k -n "$ns" annotate cluster openfga-db --overwrite cnpg.io/hibernation=on >/dev/null
  for _ in $(seq 1 60); do
    [ -z "$($k -n "$ns" get pods -l cnpg.io/cluster=openfga-db -o name)" ] && break
    sleep 5
  done
  [ -z "$($k -n "$ns" get pods -l cnpg.io/cluster=openfga-db -o name)" ] || { echo "the Cluster's pods did not go" >&2; exit 1; }
  echo "database pods gone at $(now); PVCs kept: $($k -n "$ns" get pvc -l cnpg.io/cluster=openfga-db -o name | tr '\n' ' ')"
  echo "-- a rollout while the database is away: the new pod's init container migrate cannot connect"
  $k -n "$ns" rollout restart deploy/openfga >/dev/null
  started="$(date +%s)"
  while [ $(( $(date +%s) - started )) -lt "$OUTAGE_SECONDS" ]; do
    sleep 300
    newest="$($k -n "$ns" get pods -l app.kubernetes.io/name=openfga,app.kubernetes.io/component=authz \
      --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')"
    echo "$(now) +$(( $(date +%s) - started ))s: $newest $($k -n "$ns" get pod "$newest" -o jsonpath='{.status.phase}'), migrate restarts $(migrate_restarts "$newest"), state $(migrate_state "$newest" | cut -c1-160)"
  done
  newest="$($k -n "$ns" get pods -l app.kubernetes.io/name=openfga,app.kubernetes.io/component=authz \
    --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')"
  has "$before" "$newest" && { echo "the restart made no new pod" >&2; exit 1; }
  # The Deployment is past its progress deadline by now (Progressing=False, ProgressDeadlineExceeded): a condition,
  # not a stop. The controller keeps the pod, the kubelet keeps retrying its init container, and `kubectl rollout
  # status` would exit 1 at once on that condition, so the proof watches the pod's own Ready condition instead.
  echo "-- deployment after ${OUTAGE_SECONDS} s without a database:"
  conditions
  echo "-- rehydrating the Cluster at $(now); nothing applied, nothing deleted"
  # Hibernation leaves the Cluster's Ready condition as it was, so the database's return is its primary pod Ready.
  $k -n "$ns" annotate cluster openfga-db --overwrite cnpg.io/hibernation=off >/dev/null
  for _ in $(seq 1 120); do
    [ -n "$($k -n "$ns" get pods -l cnpg.io/cluster=openfga-db -o name)" ] && break
    sleep 5
  done
  $k -n "$ns" wait --for=condition=Ready pod -l cnpg.io/cluster=openfga-db --timeout=600s >/dev/null
  back="$(date +%s)"
  echo "database Ready at $(now)"
  pod_ready "$newest" 1200
  healed="$(date +%s)"
  echo "-- $newest Ready at $(now): $(( healed - back )) s after the database came back, migrate restarts $(migrate_restarts "$newest"), found revision $(revision_found "$newest")"
  migrate_log "$newest" | cut -c1-200
  # migrate.sh waits for the database for CONNECT_TIMEOUT (600 s) before it exits 1: an outage longer than that
  # must have made the kubelet restart it at least once; a shorter trial outage is covered by that first attempt.
  if [ "$OUTAGE_SECONDS" -gt 600 ]; then
    [ "$(migrate_restarts "$newest")" -ge 1 ] || { echo "the init container never failed during the outage: the outage did not reach it" >&2; exit 1; }
  fi
  progressing_again
  running="$(openfga_pods)"
  [ "$running" = "$newest" ] || { echo "expected $newest alone running, found: $running" >&2; exit 1; }
  conditions
  echo "PASS outage: after ${OUTAGE_SECONDS} s without a database the pending pod's init container was retried by the kubelet and the pod became Ready $(( healed - back )) s after the database returned, with no apply and no delete"
}

case "$mode" in
  race) race ;;
  overlap) overlap ;;
  repair) repair ;;
  outage) outage ;;
  all) race; overlap; repair; outage ;;
esac
echo "PASS: $mode ($(now))"
