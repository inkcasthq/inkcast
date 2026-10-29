#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The longer proofs of OpenFGA's schema migration as an init container (E3.F3.9.S8, issue #337), on kind, on request:
# too long for every CI run, so they run once per change of the migration's manifests and their output is committed
# to docs/epics/E3-tenancy-authorization/evidence/openfga-recovery-e2e.txt. Each proof fails the script:
#
#   race    two pods migrate one fresh database at the same time. OpenFGA v1.22.0 takes no lock around its migrations
#           (components/openfga/deployment.yaml cites the source), so this is what the manifests must survive: a
#           fresh namespace, the Deployment scaled to two before the Cluster is ready, both init containers reach the
#           database together; both pods end Ready, the schema is at one revision, and goose's version table is
#           printed for the record. Then the Deployment goes back to one replica, the count the component ships.
#   outage  the database away for longer than the old Job's 30-minute deadline (OUTAGE_SECONDS, default 1860) while
#           a rollout is pending: the Cluster hibernated (CloudNativePG's cnpg.io/hibernation annotation: the pods
#           removed, the volume kept; docs/src/declarative_hibernation.md at v1.30.1), the Deployment restarted so the
#           new pod's init container migrate fails against no database and is retried by the kubelet; then the Cluster
#           rehydrated, and the pod becomes Ready with no apply and no delete: the time from the database's return to
#           Ready is printed, with the init container's restarts and its log.
#   all     both, race then outage (the default).
#
#   scripts/dev/openfga-recovery-e2e.sh [race|outage|all]   # after scripts/dev/openfga-e2e.sh (it installs the
#                                                           # CloudNativePG operator), on a kind cluster with the
#                                                           # inkcast:ci image loaded, as in the kind CI job
#   OUTAGE_SECONDS=120 scripts/dev/openfga-recovery-e2e.sh outage   # a shorter outage, to try the script itself
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
k="${KUBECTL:-kubectl}"
ns=inkcast-openfga
mode="${1:-all}"
OUTAGE_SECONDS="${OUTAGE_SECONDS:-1860}"
case "$mode" in race|outage|all) ;; *) echo "usage: $0 [race|outage|all]" >&2; exit 2 ;; esac

has() {  # has TEXT NEEDLE: no pipe into grep -q (under pipefail that fails at random)
  case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac
}
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
openfga_pods() {  # the running OpenFGA pods, oldest first
  $k -n "$ns" get pods -l app.kubernetes.io/name=openfga,app.kubernetes.io/component=authz --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[*].metadata.name}'
}
migrate_restarts() { $k -n "$ns" get pod "$1" -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].restartCount}'; }
migrate_state() { $k -n "$ns" get pod "$1" -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].state}'; }
revision_found() {  # revision_found POD: the schema revision the init container migrate found, from its log
  $k -n "$ns" logs "$1" -c migrate | python3 -c '
import json, sys
found = [json.loads(line).get("current version") for line in sys.stdin if line.startswith("{")]
found = [v for v in found if v is not None]
print(found[-1] if found else "none")'
}
primary() { $k -n "$ns" get pods -l cnpg.io/cluster=openfga-db,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}'; }
sql() {  # sql QUERY: on the Cluster's primary, as the operator's own `kubectl cnpg psql` does
  $k -n "$ns" exec "$(primary)" -c postgres -- psql -U postgres -d openfga -At -c "$1"
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

race() {
  echo "== race: two pods migrate one fresh database at the same time ($(now))"
  # A fresh database: the namespace goes with the Cluster and its volume; the apply makes them again. Two replicas
  # before the Cluster is ready, so both init containers wait on the same first connection and start together.
  $k delete namespace "$ns" --ignore-not-found --timeout=300s
  $k apply -k "$ROOT/deploy/kustomize/overlays/ci-openfga" >/dev/null
  $k -n "$ns" scale deploy/openfga --replicas=2
  $k -n "$ns" rollout status deploy/openfga --timeout=600s
  read -r -a pods <<< "$(openfga_pods)"
  [ "${#pods[@]}" -eq 2 ] || { echo "expected two running OpenFGA pods, found: ${pods[*]}" >&2; exit 1; }
  for pod in "${pods[@]}"; do
    echo "-- $pod: started $($k -n "$ns" get pod "$pod" -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].state.terminated.startedAt}'), finished $($k -n "$ns" get pod "$pod" -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].state.terminated.finishedAt}'), restarts $(migrate_restarts "$pod"), found revision $(revision_found "$pod")"
    $k -n "$ns" logs "$pod" -c migrate | cut -c1-200
  done
  echo "-- goose_db_version on the primary (version_id, is_applied, tstamp):"
  sql "SELECT version_id, is_applied, tstamp FROM goose_db_version ORDER BY id"
  latest="$(sql "SELECT max(version_id) FROM goose_db_version")"
  applied="$(sql "SELECT count(DISTINCT version_id) FROM goose_db_version WHERE version_id > 0")"
  files="$(sql "SELECT count(*) FROM (SELECT DISTINCT version_id FROM goose_db_version WHERE version_id > 0 AND is_applied) v")"
  echo "schema revision $latest, $applied distinct versions applied, $files of them applied"
  [ "$latest" -gt 0 ] && [ "$applied" = "$files" ] || { echo "the schema is not at one consistent revision" >&2; exit 1; }
  for pod in "${pods[@]}"; do
    ready="$($k -n "$ns" get pod "$pod" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')"
    [ "$ready" = True ] || { echo "$pod is not Ready after the race" >&2; exit 1; }
  done
  echo "both pods Ready on revision $latest; back to one replica"
  $k -n "$ns" scale deploy/openfga --replicas=1
  $k -n "$ns" rollout status deploy/openfga --timeout=300s
  echo "PASS race: two init containers migrated the same fresh database at once and both pods serve on one schema revision"
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
  $k -n "$ns" logs "$newest" -c migrate | cut -c1-200
  # migrate retries its first connection for OPENFGA_TIMEOUT (10 min) before it exits 1: an outage longer than that
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
  outage) outage ;;
  all) race; outage ;;
esac
echo "PASS: $mode ($(now))"
