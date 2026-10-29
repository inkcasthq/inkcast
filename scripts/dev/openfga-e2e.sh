#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# OpenFGA deployed with Inkcast on kind (E3.F3.9.S1, issue #279; E3.F3.9.S6, issue #315): the CloudNativePG operator
# at its pinned release (scripts/versions.env, SHA-256 checked), the ci-openfga overlay (deploy/kustomize/components/
# openfga in its own namespace, under Pod Security "restricted"), then the proofs the stories ask for, each one
# failing the script:
#
#   1. the init container migrate of OpenFGA's pod brings a fresh database to the current schema (E3.F3.9.S8, issue
#      #337: no Job) and the OpenFGA Deployment rolls out;
#   2. the init container authz-model of Inkcast's pod, restarted by the kubelet until OpenFGA serves, creates the
#      store and writes the model with no manual step, and the server then pins that store and version by name: the
#      Deployment rolls out on its own; nothing patches the ConfigMap, nothing runs write-model by hand;
#   3. /readyz passes against OpenFGA, and /secrets shows the key was read from the mounted file (its name and
#      source, never its value);
#   4. a restarted pod (`rollout restart`) runs the init container again, which finds the model unchanged: the same
#      ids, pinned again, one store, one version;
#   5. an image change on OpenFGA's Deployment re-runs the migration with no manual step: the new pod's init
#      container finds the schema current and the server starts; then the apply of the manifests over the changed
#      template is accepted (a Job's template would have refused it: "field is immutable").
#
#   scripts/dev/openfga-e2e.sh        # needs a kind cluster with the inkcast:ci image loaded, as in the kind CI job
#   scripts/dev/openfga-recovery-e2e.sh runs the longer proofs of #337 (two pods migrating a fresh database at once,
#   a database outage longer than 30 minutes) on request, not in CI.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/versions.env"
k="${KUBECTL:-kubectl}"
ns=inkcast-openfga
work="$(mktemp -d)"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; rm -rf "$work"; }
trap cleanup EXIT
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
forward() {  # service local:remote; background, stopped on exit
  $k -n "$ns" port-forward "svc/$1" "$2" >/dev/null 2>&1 &
  PIDS+=("$!")
}
server_pod() {  # the one server pod: Recreate removes the old pod before it makes the new one
  local pods
  pods="$($k -n "$ns" get pods -l app.kubernetes.io/component=server -o jsonpath='{.items[*].metadata.name}')"
  [ "$(wc -w <<< "$pods")" -eq 1 ] || { echo "expected one server pod, found: $pods" >&2; return 1; }
  echo "$pods"
}
has() {  # has TEXT NEEDLE: no pipe into grep -q (under pipefail that fails at random)
  case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac
}
openfga_pod() {  # the newest running OpenFGA pod (a rollout leaves the old one Terminating for a while)
  $k -n "$ns" get pods -l app.kubernetes.io/name=openfga,app.kubernetes.io/component=authz --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}'
}
migrated() {  # migrated POD: the init container's log, which must report the revision it found and a finished run
  local log
  log="$($k -n "$ns" logs "$1" -c migrate)"; echo "$log"
  has "$log" '"current version":' || { echo "$1: the init container migrate did not report the schema revision" >&2; return 1; }
  has "$log" '"migration done"' || { echo "$1: the init container migrate did not finish" >&2; return 1; }
}

echo "== CloudNativePG $CNPG_VERSION (the release manifest at its tag, checked against scripts/versions.env)"
curl -fsSL --retry 3 -o "$work/cnpg.yaml" \
  "https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/v${CNPG_VERSION}/releases/cnpg-${CNPG_VERSION}.yaml"
got="$(sha256 "$work/cnpg.yaml")"
if [ "$got" != "$CNPG_MANIFEST_SHA256" ]; then
  echo "cnpg-${CNPG_VERSION}.yaml: SHA-256 $got is not the pinned $CNPG_MANIFEST_SHA256; not applied" >&2; exit 1
fi
# Server-side apply, as the operator's own installation guide says: its CRDs exceed the last-applied annotation.
$k apply --server-side -f "$work/cnpg.yaml" >/dev/null
$k -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=300s

echo "== Inkcast with the OpenFGA component (ci-openfga)"
$k apply -k "$ROOT/deploy/kustomize/overlays/ci-openfga"
echo "-- 1. the init container migrate brings the fresh database to the current schema, then the server starts"
# One rollout covers the Cluster's bootstrap (migrate retries its first connection for OPENFGA_TIMEOUT), the
# migration and the server's startup probe; the Deployment's progress deadline is the default 600 s.
$k -n "$ns" rollout status deploy/openfga --timeout=600s
$k -n "$ns" get cluster,pods -o wide | cut -c1-120
fresh="$(openfga_pod)"
log="$(migrated "$fresh")"; cut -c1-200 <<< "$log"
has "$log" '"current version":0' || { echo "a fresh database should start at revision 0; the log says otherwise" >&2; exit 1; }
echo "migrate on $fresh: a fresh database, migrated, restarts $($k -n "$ns" get pod "$fresh" -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].restartCount}')"

echo "-- 2. the init container authz-model writes the store and the model; Inkcast pins them by name and rolls out on its own"
# The init container failed and was restarted (the kubelet's back-off) while OpenFGA was not serving; it completes
# once it is, and only then does the server start. Its log holds what the run did and the ids it printed. Nothing
# here pins them: the server reads them from the store at start-up. The Deployment's progress deadline is 900 s.
$k -n "$ns" rollout status deploy/inkcast --timeout=900s
pod="$(server_pod)"
first="$($k -n "$ns" logs "$pod" -c authz-model)"; echo "$first"
has "$first" "model written" || { echo "the init container did not write the model" >&2; exit 1; }
eval "$(grep -E '^INKCAST_FGA_(STORE|MODEL)_ID=[0-9A-HJKMNP-TV-Z]{26}$' <<< "$first" || true)"   # the two ids, as printed
[ -n "${INKCAST_FGA_STORE_ID:-}" ] && [ -n "${INKCAST_FGA_MODEL_ID:-}" ] || { echo "the init container printed no ids" >&2; exit 1; }
needle="model version $INKCAST_FGA_MODEL_ID is this release's packaged model and is pinned"
server="$($k -n "$ns" logs "$pod" -c inkcast --tail=200)"
has "$server" "$needle" || { echo "Inkcast did not log the pinned version; last log:" >&2; tail -20 <<< "$server" >&2; exit 1; }
grep -F "is pinned" <<< "$server" | tail -1 | cut -c1-220
echo "restarts: the init container $($k -n "$ns" get pod "$pod" -o jsonpath='{.status.initContainerStatuses[0].restartCount}') (until OpenFGA served), the server $($k -n "$ns" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')"

echo "-- 3. ready against OpenFGA; the key was read from the mounted file"
forward inkcast-metrics 29090:9090
for _ in $(seq 1 30); do curl -sf localhost:29090/readyz >/dev/null && break; sleep 1; done
curl -sf localhost:29090/readyz | python3 -c '
import json, sys
body = json.load(sys.stdin)
print("/readyz:", {k: body.get(k) for k in ("ok", "problems")})
sys.exit(0 if body.get("ok") is True else 1)'
curl -sf localhost:29090/secrets | python3 -c '
import json, sys
names = {s["name"]: s for s in json.load(sys.stdin)["secrets"]}
got = names.get("INKCAST_FGA_API_TOKEN") or {}
print("INKCAST_FGA_API_TOKEN:", {k: got.get(k) for k in ("present", "source")})
sys.exit(0 if got.get("present") else "the key was not read")'
engine="$($k -n "$ns" get configmap inkcast-config -o jsonpath='{.data.INKCAST_AUTHZ_ENGINE}')"
[ "$engine" = openfga ] || { echo "INKCAST_AUTHZ_ENGINE is $engine" >&2; exit 1; }
for id in INKCAST_FGA_STORE_ID INKCAST_FGA_MODEL_ID; do
  pinned_by_hand="$($k -n "$ns" get configmap inkcast-config -o jsonpath="{.data.$id}")"
  [ -z "$pinned_by_hand" ] || { echo "$id is in the ConfigMap ($pinned_by_hand): the pods should pin by name" >&2; exit 1; }
done

echo "-- 4. a restarted pod runs the init container again: the model unchanged, the same ids, pinned again"
$k -n "$ns" rollout restart deploy/inkcast
$k -n "$ns" rollout status deploy/inkcast --timeout=600s
again="$(server_pod)"
[ "$again" != "$pod" ] || { echo "the rollout left the same pod ($pod)" >&2; exit 1; }
second="$($k -n "$ns" logs "$again" -c authz-model)"; echo "$second"
has "$second" "model unchanged" || { echo "the second run did not find the model unchanged" >&2; exit 1; }
if ! has "$second" "INKCAST_FGA_STORE_ID=$INKCAST_FGA_STORE_ID" || ! has "$second" "INKCAST_FGA_MODEL_ID=$INKCAST_FGA_MODEL_ID"; then
  echo "the second run printed different ids" >&2; exit 1
fi
server="$($k -n "$ns" logs "$again" -c inkcast --tail=200)"
has "$server" "$needle" || { echo "the restarted pod did not pin the same version; last log:" >&2; tail -20 <<< "$server" >&2; exit 1; }
# The store as the server holds it: one store of the name, one model version, through the port-forward with the key
# read back from the Secret into a header file for curl (never on a command line).
$k -n "$ns" get secret inkcast-secrets -o jsonpath='{.data.fga-api-token}' | base64 -d > "$work/fga-api-token"
printf 'Authorization: Bearer %s\n' "$(cat "$work/fga-api-token")" > "$work/auth-header"
forward openfga 28080:8080
for _ in $(seq 1 30); do curl -sf localhost:28080/healthz >/dev/null && break; sleep 1; done
stores="$(curl -sf -H @"$work/auth-header" "localhost:28080/stores?name=inkcast&page_size=100" \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["stores"]))')"
versions="$(curl -sf -H @"$work/auth-header" \
  "localhost:28080/stores/$INKCAST_FGA_STORE_ID/authorization-models?page_size=100" \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["authorization_models"]))')"
[ "$stores" = 1 ] && [ "$versions" = 1 ] || { echo "expected one store and one version after two runs, found $stores and $versions" >&2; exit 1; }
echo "one store, one model version after two pod starts"

echo "-- 5. an image change re-runs the migration with no manual step, and the next apply is accepted"
# The same build named by its tag alone is a changed pod template: a new ReplicaSet, a new pod whose init container
# migrates (and finds the schema current) before its server starts, while the old pod serves. Then the manifests'
# apply, which puts the digest back, is accepted over the changed template: nothing immutable, nothing to delete.
pinned="$($k -n "$ns" get deploy openfga -o jsonpath='{.spec.template.spec.containers[0].image}')"
has "$pinned" "@sha256:" || { echo "the server's image is not pinned by digest: $pinned" >&2; exit 1; }
$k -n "$ns" set image deploy/openfga "migrate=${pinned%@*}" "openfga=${pinned%@*}"
$k -n "$ns" rollout status deploy/openfga --timeout=600s
changed="$(openfga_pod)"
[ "$changed" != "$fresh" ] || { echo "the image change left the same pod ($fresh)" >&2; exit 1; }
log="$(migrated "$changed")"; cut -c1-200 <<< "$log"
has "$log" '"current version":0' && { echo "the re-run found an empty database: the schema was lost" >&2; exit 1; }
echo "migrate ran again on $changed, on the current schema"
$k apply -k "$ROOT/deploy/kustomize/overlays/ci-openfga"   # over the changed template: accepted
$k -n "$ns" rollout status deploy/openfga --timeout=600s
[ "$($k -n "$ns" get deploy openfga -o jsonpath='{.spec.template.spec.containers[0].image}')" = "$pinned" ] \
  || { echo "the apply did not put the pinned image back" >&2; exit 1; }
restored="$(openfga_pod)"
[ "$restored" != "$changed" ] || { echo "the apply left the same pod ($changed)" >&2; exit 1; }
migrated "$restored" >/dev/null
echo "the apply over the changed template was accepted and rolled out; migrate ran a third time on $restored"
echo "PASS: OpenFGA on CloudNativePG under restricted, its init container migrated a fresh database, the init container authz-model wrote the store and the model with no manual step, Inkcast pinned them by name and is ready, a restarted pod found the model unchanged, and an image change re-ran the migration with no manual step"
