#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The production Keycloak component on kind (E9.F9.1.S3, issue #386): the CloudNativePG operator and the Keycloak
# operator at their pinned releases (scripts/versions.env, every install file checked against its SHA-256), the
# ci-keycloak-production overlay (deploy/kustomize/components/keycloak-production in namespace keycloak, under Pod
# Security "restricted"), then the lab's own realm imports (deploy/kustomize/shared/keycloak-realms), and the proofs
# the story asks for, each one failing the script:
#
#   1. the Keycloak CR becomes Ready with two pods, started with `start` (not `start-dev`, no `--optimized`), the
#      database settings as Secret references to the Secret CloudNativePG generated, nothing as a literal;
#   2. both KeycloakRealmImports (inkcast-staging, inkcast-prod) report Done and no error;
#   3. the realms are rows of the CloudNativePG database, and every connection Keycloak holds to it is TLS;
#   4. the discovery document of each realm is served over HTTPS with the patched hostname as its issuer;
#   5. a pod deleted and recreated serves the realm again: the state is in PostgreSQL, not in memory.
#
# What kind cannot prove: NetworkPolicy enforcement (kindnet enforces none), so the policy is checked as a rendered
# object by tests/test_keycloak_production_component.py, and the Ingress (no controller). No Secret value is ever
# printed: the realm Secrets hold random values made here, and the TLS Secret a self-signed certificate made here.
#
#   scripts/dev/keycloak-production-e2e.sh    # on a kind cluster, as in the kind CI job (the inkcast image is not
#                                             # needed); installs CloudNativePG if scripts/dev/openfga-e2e.sh has not
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/versions.env"
k="${KUBECTL:-kubectl}"
ns=keycloak
work="$(mktemp -d)"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; rm -rf "$work"; }
trap cleanup EXIT
sha256() { (command -v sha256sum >/dev/null && sha256sum "$1" || shasum -a 256 "$1") | cut -d' ' -f1; }
has() {  # has TEXT NEEDLE: no pipe into grep -q (under pipefail that fails at random)
  case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac
}
rand() { python3 -c 'import secrets; print("k" + secrets.token_urlsafe(24), end="")'; }
fetch_pinned() {  # fetch_pinned URL FILE SHA256: download, compare, refuse what differs
  curl -fsSL --retry 3 -o "$2" "$1"
  local got; got="$(sha256 "$2")"
  if [ "$got" != "$3" ]; then echo "$(basename "$2"): SHA-256 $got is not the pinned $3; not applied" >&2; exit 1; fi
}
psql_keycloak() {  # a read-only query against the keycloak database, as postgres over the instance's local socket
  $k -n "$ns" exec keycloak-db-1 -c postgres -- psql -U postgres -d keycloak -tAc "$1"
}
discovery() {  # discovery REALM: the realm's OIDC discovery document through the HTTPS port-forward, parsed
  curl -sfk "https://localhost:28443/realms/$1/.well-known/openid-configuration" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
print(doc["issuer"])'
}

echo "== CloudNativePG $CNPG_VERSION (the release manifest at its tag, checked against scripts/versions.env)"
if $k -n cnpg-system get deploy cnpg-controller-manager >/dev/null 2>&1; then
  echo "already installed (scripts/dev/openfga-e2e.sh)"
else
  fetch_pinned "https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/v${CNPG_VERSION}/releases/cnpg-${CNPG_VERSION}.yaml" \
    "$work/cnpg.yaml" "$CNPG_MANIFEST_SHA256"
  $k apply --server-side -f "$work/cnpg.yaml" >/dev/null   # server-side, as the operator's guide says: the CRDs exceed the last-applied annotation
fi
$k -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=300s

echo "== Keycloak operator $KEYCLOAK_OPERATOR_VERSION (keycloak-k8s-resources at its tag, each file checked against scripts/versions.env)"
base="https://raw.githubusercontent.com/keycloak/keycloak-k8s-resources/${KEYCLOAK_OPERATOR_VERSION}/kubernetes"
fetch_pinned "$base/keycloaks.k8s.keycloak.org-v1.yml" "$work/crd-keycloaks.yml" "$KEYCLOAK_OPERATOR_CRD_KEYCLOAKS_SHA256"
fetch_pinned "$base/keycloakrealmimports.k8s.keycloak.org-v1.yml" "$work/crd-realmimports.yml" "$KEYCLOAK_OPERATOR_CRD_REALMIMPORTS_SHA256"
mkdir -p "$work/operator"
fetch_pinned "$base/kubernetes.yml" "$work/operator/kubernetes.yml" "$KEYCLOAK_OPERATOR_MANIFEST_SHA256"
$k apply --server-side -f "$work/crd-keycloaks.yml" -f "$work/crd-realmimports.yml" >/dev/null
$k wait --for=condition=Established crd/keycloaks.k8s.keycloak.org crd/keycloakrealmimports.k8s.keycloak.org --timeout=60s
echo "-- the component (namespace keycloak, the CR, its Cluster and NetworkPolicy)"
$k apply -k "$ROOT/deploy/kustomize/overlays/ci-keycloak-production"
# The upstream install file is namespace-less (applied with -n) and its operator Deployment carries no
# securityContext, which Pod Security "restricted" requires on kind: the same two blocks the overlay adds to the
# server's pods. The operator image runs as uid 1000 (operator/Dockerfile at the tag), so no runAsUser.
cat > "$work/operator/kustomization.yaml" <<YAML
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $ns
resources: [kubernetes.yml]
patches:
  - target: {kind: Deployment, name: keycloak-operator}
    patch: |-
      - op: add
        path: /spec/template/spec/securityContext
        value: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
      - op: add
        path: /spec/template/spec/containers/0/securityContext
        value: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
YAML
$k apply -k "$work/operator" >/dev/null
$k -n "$ns" rollout status deploy/keycloak-operator --timeout=300s
echo "-- the server's certificate: a self-signed one for the in-cluster Service name (never committed, never printed)"
openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=keycloak-service.$ns.svc" \
  -keyout "$work/tls.key" -out "$work/tls.crt" >/dev/null 2>&1
$k -n "$ns" create secret tls keycloak-tls --cert="$work/tls.crt" --key="$work/tls.key" --dry-run=client -o yaml | $k apply -f - >/dev/null

echo "-- 1. the database Ready, then the Keycloak CR Ready with two pods"
$k -n "$ns" wait --for=condition=Ready cluster/keycloak-db --timeout=600s
$k -n "$ns" wait --for=condition=Ready keycloak/keycloak --timeout=900s   # the first start augments the distribution
$k -n "$ns" get keycloak keycloak -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'
ready="$($k -n "$ns" get statefulset keycloak -o jsonpath='{.status.readyReplicas}/{.spec.replicas}')"
[ "$ready" = "2/2" ] || { echo "expected 2/2 ready replicas, found $ready" >&2; exit 1; }
args="$($k -n "$ns" get statefulset keycloak -o jsonpath='{.spec.template.spec.containers[0].args}')"
echo "args: $args"
has "$args" '"start"' || { echo "the server is not started with \`start\`" >&2; exit 1; }
if has "$args" "start-dev" || has "$args" "optimized"; then echo "unexpected start mode: $args" >&2; exit 1; fi
# The database settings: each a Secret reference to the Secret CloudNativePG wrote, nothing as a literal; printed
# as names only (the env entries' valueFrom, never a value).
$k -n "$ns" get statefulset keycloak -o json | python3 -c '
import json, sys
env = {e["name"]: e for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]["env"]}
want = {"KC_DB": "postgres", "KC_DB_URL_HOST": "keycloak-db-rw", "KC_DB_URL_DATABASE": "keycloak", "KC_DB_URL_PORT": "5432",
        "KC_DB_URL_PROPERTIES": "?sslmode=require"}
got = {k: env.get(k, {}).get("value") for k in want}
refs = {k: env.get(k, {}).get("valueFrom", {}).get("secretKeyRef") for k in ("KC_DB_USERNAME", "KC_DB_PASSWORD")}
print("database settings:", got)
print("credentials:", refs)
sys.exit(0 if got == want and refs == {"KC_DB_USERNAME": {"name": "keycloak-db-app", "key": "username"},
                                       "KC_DB_PASSWORD": {"name": "keycloak-db-app", "key": "password"}}
        else "the database settings are not the component'"'"'s")'
$k -n "$ns" get pods -l app=keycloak -o custom-columns='POD:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount,RUN_AS_NON_ROOT:.spec.securityContext.runAsNonRoot,SECCOMP:.spec.securityContext.seccompProfile.type'
for pod in keycloak-0 keycloak-1; do   # memory in use (cgroup v2), to size the CR's request: a number, not a guess
  mem="$($k -n "$ns" exec "$pod" -c keycloak -- cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)"
  echo "$pod memory.current: $((mem / 1048576)) MiB"
done

echo "-- 2. the lab's realm imports complete into this Keycloak (shared/keycloak-realms, Secrets made here)"
for side in staging prod; do
  $k -n "$ns" create secret generic "inkcast-$side-realm" --from-literal=identity-client-secret="$(rand)" \
    --from-literal=sync-client-secret="$(rand)" --dry-run=client -o yaml | $k apply -f - >/dev/null
done
$k apply -k "$ROOT/deploy/kustomize/shared/keycloak-realms"
for realm in inkcast-staging inkcast-prod; do
  $k -n "$ns" wait --for=condition=Done "keycloakrealmimport/$realm" --timeout=600s
  $k -n "$ns" get keycloakrealmimport "$realm" -o go-template='{{range .status.conditions}}CONDITION: {{.type}}  STATUS: {{.status}}  MESSAGE: {{.message}}{{"\n"}}{{end}}'
  errors="$($k -n "$ns" get keycloakrealmimport "$realm" -o jsonpath='{.status.conditions[?(@.type=="HasErrors")].status}')"
  [ "$errors" = "False" ] || { echo "$realm: HasErrors=$errors" >&2; exit 1; }
done
$k -n "$ns" get jobs -l app.kubernetes.io/managed-by=keycloak-operator

echo "-- 3. the realms are in the CloudNativePG database, and every connection Keycloak holds to it is TLS"
realms="$(psql_keycloak "select string_agg(name, ' ' order by name) from realm")"
echo "realms in PostgreSQL: $realms"
for realm in master inkcast-staging inkcast-prod; do has "$realms" "$realm" || { echo "$realm is not in the database" >&2; exit 1; }; done
ssl="$(psql_keycloak "select count(*) filter (where s.ssl) || '/' || count(*) from pg_stat_activity a join pg_stat_ssl s using (pid) where a.usename = 'keycloak'")"
echo "keycloak's connections, TLS/all: $ssl"
case "$ssl" in 0/*|*/0) echo "expected TLS connections from keycloak, found $ssl" >&2; exit 1 ;; esac
[ "${ssl%/*}" = "${ssl#*/}" ] || { echo "a connection from keycloak is not TLS: $ssl" >&2; exit 1; }

echo "-- 4. the discovery document over HTTPS, with the patched hostname as the issuer"
$k -n "$ns" port-forward svc/keycloak-service 28443:8443 >/dev/null 2>&1 &
PIDS+=("$!")
for _ in $(seq 1 30); do curl -sfk -o /dev/null https://localhost:28443/realms/master && break; sleep 1; done
hostname="$($k -n "$ns" get keycloak keycloak -o jsonpath='{.spec.hostname.hostname}')"
for realm in inkcast-staging inkcast-prod; do
  issuer="$(discovery "$realm")"
  echo "$realm issuer: $issuer"
  [ "$issuer" = "$hostname/realms/$realm" ] || { echo "expected issuer $hostname/realms/$realm" >&2; exit 1; }
done

echo "-- 5. a pod deleted and recreated serves the realm again: the state is in PostgreSQL"
$k -n "$ns" delete pod keycloak-0 --wait=true >/dev/null
$k -n "$ns" wait --for=condition=Ready pod/keycloak-0 --timeout=600s
$k -n "$ns" wait --for=condition=Ready keycloak/keycloak --timeout=300s
issuer="$(discovery inkcast-staging)"
[ "$issuer" = "$hostname/realms/inkcast-staging" ] || { echo "after the restart: issuer $issuer" >&2; exit 1; }
echo "keycloak-0 recreated, inkcast-staging served again"
echo "PASS: the Keycloak operator and CloudNativePG at their pinned releases, the component under restricted, the CR Ready with two pods on the CloudNativePG database, both realm imports Done, the realms in PostgreSQL over TLS, discovery over HTTPS, and a recreated pod serving them again"
