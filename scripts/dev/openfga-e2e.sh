#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# OpenFGA deployed with Inkcast on kind (E3.F3.9.S1, issue #279; E3.F3.9.S6, issue #315): the CloudNativePG operator
# at its pinned release (scripts/versions.env, SHA-256 checked), the ci-openfga overlay (deploy/kustomize/components/
# openfga in its own namespace, under Pod Security "restricted"), then the proofs the stories ask for, each one
# failing the script:
#
#   1. the migrate Job completes and the OpenFGA Deployment rolls out;
#   2. the Job inkcast-authz-model, applied with the overlay, creates the store and writes the model with no manual
#      step, and Inkcast's pods, which refused to start until it had, pin that store and version by name and roll
#      out on their own: nothing patches the ConfigMap, nothing runs write-model by hand;
#   3. /readyz passes against OpenFGA, and /secrets shows the key was read from the mounted file (its name and
#      source, never its value);
#   4. the finished Job is gone after its TTL, a second apply of the same release creates and runs it again, with
#      no immutable-field error, and that run finds the model unchanged: one store, one version, the same ids.
#
#   scripts/dev/openfga-e2e.sh        # needs a kind cluster with the inkcast:ci image loaded, as in the kind CI job
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
sha256() { (command -v sha256sum >/dev/null && sha256sum "$1" || shasum -a 256 "$1") | cut -d' ' -f1; }
forward() {  # service local:remote; background, stopped on exit
  $k -n "$ns" port-forward "svc/$1" "$2" >/dev/null 2>&1 &
  PIDS+=("$!")
}
job_log() {  # the Job's current container: the run that completed (earlier attempts, restarted in place, are --previous)
  $k -n "$ns" logs job/inkcast-authz-model
}
has() {  # has TEXT NEEDLE: no pipe into grep -q (under pipefail that fails at random)
  case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac
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
echo "-- 1. the migrate Job completes, then the server rolls out"
$k -n "$ns" wait --for=condition=complete job/openfga-migrate --timeout=600s
$k -n "$ns" rollout status deploy/openfga --timeout=300s
$k -n "$ns" get cluster,job,pods -o wide | cut -c1-120

echo "-- 2. the Job inkcast-authz-model writes the store and the model; Inkcast pins them by name and rolls out on its own"
# The Job failed and was restarted (with the kubelet's backoff) while OpenFGA was not serving; it completes once it
# is. Its log holds what the run did and the ids it printed. Nothing here pins them: the pods, refusing to start and
# naming the Job meanwhile, read them from the store at start-up.
$k -n "$ns" wait --for=condition=complete job/inkcast-authz-model --timeout=600s
first="$(job_log)"; echo "$first"
has "$first" "model written" || { echo "the first run did not write the model" >&2; exit 1; }
eval "$(grep -E '^INKCAST_FGA_(STORE|MODEL)_ID=[0-9A-HJKMNP-TV-Z]{26}$' <<< "$first" || true)"   # the two ids, as printed
[ -n "${INKCAST_FGA_STORE_ID:-}" ] && [ -n "${INKCAST_FGA_MODEL_ID:-}" ] || { echo "the Job printed no ids" >&2; exit 1; }
$k -n "$ns" rollout status deploy/inkcast --timeout=600s
server="$($k -n "$ns" logs deploy/inkcast --tail=200)"
needle="model version $INKCAST_FGA_MODEL_ID is this release's packaged model and is pinned"
has "$server" "$needle" || { echo "Inkcast did not log the pinned version; last log:" >&2; tail -20 <<< "$server" >&2; exit 1; }
grep -F "is pinned" <<< "$server" | tail -1 | cut -c1-220
restarts="$($k -n "$ns" get pods -l app.kubernetes.io/component=server -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')"
echo "server pod restarts before it could pin (each a named refusal, exit 78): $restarts"

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

echo "-- 4. a second apply runs the Job again (the finished one is gone after its TTL) and changes nothing"
ttl="$(grep -Eo 'ttlSecondsAfterFinished: [0-9]+' "$ROOT/deploy/kustomize/components/openfga/authz-model.yaml" | cut -d' ' -f2)"
deadline=$((SECONDS + ttl + 120))
while $k -n "$ns" get job/inkcast-authz-model >/dev/null 2>&1; do
  [ $SECONDS -lt $deadline ] || { echo "the finished Job was not deleted within $((ttl + 120)) s (ttlSecondsAfterFinished=$ttl)" >&2; exit 1; }
  sleep 5
done
echo "the finished Job is gone (ttlSecondsAfterFinished=$ttl): the next apply creates it again"
$k apply -k "$ROOT/deploy/kustomize/overlays/ci-openfga"
$k -n "$ns" wait --for=condition=complete job/inkcast-authz-model --timeout=300s
second="$(job_log)"; echo "$second"
has "$second" "model unchanged" || { echo "the second run did not find the model unchanged" >&2; exit 1; }
if ! has "$second" "INKCAST_FGA_STORE_ID=$INKCAST_FGA_STORE_ID" || ! has "$second" "INKCAST_FGA_MODEL_ID=$INKCAST_FGA_MODEL_ID"; then
  echo "the second run printed different ids" >&2; exit 1
fi
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
echo "one store, one model version after two applies"
echo "PASS: OpenFGA on CloudNativePG under restricted, the migrate Job done, the authz-model Job wrote the store and the model with no manual step, Inkcast pinned them by name and is ready, and a second apply ran the Job again unchanged"
