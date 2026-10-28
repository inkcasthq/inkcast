#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# OpenFGA deployed with Inkcast on kind (E3.F3.9.S1, issue #279): the CloudNativePG operator at its pinned release
# (scripts/versions.env, SHA-256 checked), the ci-openfga overlay (deploy/kustomize/components/openfga in its own
# namespace, under Pod Security "restricted"), then the proofs the story asks for, each one failing the script:
#
#   1. the migrate Job completes and the OpenFGA Deployment rolls out;
#   2. Inkcast refuses to start while the store and model ids are not pinned, and its log names them;
#   3. `inkcast authz write-model` (through a port-forward, with the key read back from the cluster's Secret)
#      creates the store and writes the model; a second run prints the same ids and writes nothing;
#   4. with both ids pinned in the ConfigMap, Inkcast rolls out and /readyz passes against OpenFGA, and /secrets
#      shows the key was read from the mounted file (its name and source, never its value).
#
#   scripts/dev/openfga-e2e.sh        # needs a kind cluster with the inkcast:ci image loaded and `inkcast` installed
#                                     # (pip install -e .), as in the kind CI job
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

echo "-- 2. Inkcast refuses to start until the ids are pinned, and names them"
needle="INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID"
deadline=$((SECONDS + 180)); named=""
while [ $SECONDS -lt $deadline ]; do
  log="$($k -n "$ns" logs deploy/inkcast --tail=40 2>/dev/null || true)"
  case "$log" in *"$needle"*) named=1; break ;; esac   # no pipe into grep -q: under pipefail that fails at random
  sleep 3
done
[ -n "$named" ] || { echo "Inkcast did not name the unpinned ids within 180 s; last log:" >&2; echo "$log" >&2; exit 1; }
grep -F "$needle" <<< "$log" | tail -1 | cut -c1-200

echo "-- 3. inkcast authz write-model, twice"
# The key as the pods hold it, read back from the Secret into a file the command reads as INKCAST_FGA_API_TOKEN_FILE,
# and into a header file for curl (never on a command line).
$k -n "$ns" get secret inkcast-secrets -o jsonpath='{.data.fga-api-token}' | base64 -d > "$work/fga-api-token"
printf 'Authorization: Bearer %s\n' "$(cat "$work/fga-api-token")" > "$work/auth-header"
forward openfga 28080:8080
for _ in $(seq 1 30); do curl -sf localhost:28080/healthz >/dev/null && break; sleep 1; done
curl -sf localhost:28080/healthz | python3 -c 'import json,sys; s=json.load(sys.stdin)["status"]; print("openfga /healthz:", s); sys.exit(0 if s=="SERVING" else 1)'
write_model() { INKCAST_FGA_API_URL=http://localhost:28080 INKCAST_FGA_API_TOKEN_FILE="$work/fga-api-token" inkcast authz write-model; }
first="$(write_model)"; echo "$first"
second="$(write_model)"
[ "$first" = "$second" ] || { echo "the second run printed different ids:" >&2; echo "$second" >&2; exit 1; }
# Exactly the two lines, each a ULID (26 chars of Crockford base32), before they are evaluated as assignments.
ids="$(grep -Ec '^INKCAST_FGA_(STORE|MODEL)_ID=[0-9A-HJKMNP-TV-Z]{26}$' <<< "$first" || true)"
[ "$ids" = 2 ] && [ "$(wc -l <<< "$first")" -eq 2 ] || { echo "write-model printed something other than the two ids:" >&2; echo "$first" >&2; exit 1; }
eval "$first"   # INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID, as printed
versions="$(curl -sf -H @"$work/auth-header" \
  "localhost:28080/stores/$INKCAST_FGA_STORE_ID/authorization-models?page_size=100" \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["authorization_models"]))')"
[ "$versions" = 1 ] || { echo "expected one model version after two runs, found $versions" >&2; exit 1; }
echo "one store, one model version after two runs"

echo "-- 4. pinned: Inkcast rolls out and is ready against OpenFGA"
$k -n "$ns" patch configmap inkcast-config --type merge \
  -p "{\"data\":{\"INKCAST_FGA_STORE_ID\":\"$INKCAST_FGA_STORE_ID\",\"INKCAST_FGA_MODEL_ID\":\"$INKCAST_FGA_MODEL_ID\"}}"
$k -n "$ns" rollout restart deploy/inkcast
$k -n "$ns" rollout status deploy/inkcast --timeout=180s
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
echo "PASS: OpenFGA on CloudNativePG under restricted, the migrate Job done, ids written once and pinned, Inkcast ready"
