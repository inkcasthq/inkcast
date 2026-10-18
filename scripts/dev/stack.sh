#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The local observability stack (deploy/dev): Inkcast + fake platforms + Prometheus + Grafana, then seed real
# traffic and check every dashboard panel against live data (scripts/dev/dashboards.py).
#
#   scripts/dev/stack.sh compose        # Docker Compose: build, start, seed, check, screenshot
#   scripts/dev/stack.sh compose-down   # stop and delete its volumes
#   scripts/dev/stack.sh kind           # kind: cluster "inkcast" (made if missing), build and load images,
#                                       # apply deploy/dev, port-forward, seed, check, screenshot
#   scripts/dev/stack.sh kind-down      # delete the dev resources (the cluster stays)
#   scripts/dev/stack.sh secrets        # only make deploy/dev/.secrets (random, not committed)
#
# Then open Grafana at http://localhost:3000 (light theme; anonymous viewer). The check writes
# deploy/dev/.captures/{summary.md,summary.json,inkcast-platform.light.png}. SEED=0 skips the seed;
# CHECK=0 skips the check (to leave the stack running for a look).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEV="$ROOT/deploy/dev"
SECRETS="$DEV/.secrets"
CLUSTER="${KIND_CLUSTER:-inkcast}"
PIDS=()

secrets() {
  mkdir -p "$SECRETS"
  for name in secret-key password grafana-admin-password; do
    [ -s "$SECRETS/$name" ] || python3 -c 'import secrets; print(secrets.token_urlsafe(36), end="")' > "$SECRETS/$name"
  done
  # Compose mounts these into containers that run as other users (1001, 472), so they must be readable; the
  # Inkcast posture check warns about that in development, which is expected here.
  chmod 0644 "$SECRETS"/*
}

seed_and_check() {
  local grafana="$1" prometheus="$2"
  if [ "${SEED:-1}" = 1 ]; then
    python3 "$ROOT/scripts/dev/dashboards.py" seed --url http://localhost:8787 --password-file "$SECRETS/password"
  fi
  if [ "${CHECK:-1}" = 1 ]; then
    python3 "$ROOT/scripts/dev/dashboards.py" check --grafana "$grafana" --prometheus "$prometheus" \
      --out "$DEV/.captures"
  fi
}

compose() { docker compose -f "$DEV/compose.yaml" "$@"; }

forward() {  # namespace service local:remote; background, stopped on exit
  kubectl -n "$1" port-forward "svc/$2" "$3" >/dev/null 2>&1 &
  PIDS+=("$!")
}
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

case "${1:-}" in
  secrets) secrets ;;
  compose)
    secrets
    compose up -d --build
    seed_and_check http://localhost:3000 http://localhost:9091
    echo "Grafana: http://localhost:3000   Prometheus: http://localhost:9091   Inkcast: http://localhost:8787"
    ;;
  compose-down) compose down -v ;;
  kind)
    secrets
    kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 120s
    if [ "${BUILD:-1}" = 1 ]; then
      docker build -t inkcast:ci "$ROOT"
    fi
    docker build -f "$DEV/fakes.Dockerfile" --build-arg BASE=inkcast:ci -t inkcast-fakes:dev "$ROOT"
    kind load docker-image inkcast:ci inkcast-fakes:dev --name "$CLUSTER"
    kubectl apply -k "$DEV"
    kubectl -n inkcast rollout restart deploy/inkcast            # pick up the merged dev config and secret
    kubectl -n inkcast rollout status deploy/inkcast-fakes --timeout=180s
    kubectl -n inkcast rollout status deploy/inkcast --timeout=180s
    kubectl -n monitoring rollout status deploy/prometheus --timeout=180s
    kubectl -n monitoring rollout status deploy/grafana --timeout=180s
    forward inkcast inkcast 8787:8787
    forward monitoring prometheus 9091:9090
    forward monitoring grafana 3000:3000
    for _ in $(seq 1 30); do curl -sf localhost:8787/livez >/dev/null && break; sleep 1; done
    seed_and_check http://localhost:3000 http://localhost:9091
    echo "Port-forwards end with this script; for a look: kubectl -n monitoring port-forward svc/grafana 3000:3000"
    ;;
  kind-down)
    kubectl delete -k "$DEV" --ignore-not-found
    ;;
  *)
    sed -n '4,17p' "$0"; exit 2 ;;
esac
