#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The shared OpenFGA database on a running lab (E3.F3.9.S5, issue #296): what shared/inkcast-db and the two
# environments promise, measured on the cluster itself. Each proof fails the script:
#
#   1. the CloudNativePG cluster inkcast-pg is healthy;
#   2. openfga_staging and openfga_prod exist, each owned by its own role (the catalog, not only the Database objects);
#   3. each environment's OpenFGA is migrated in its own database: its openfga-db-app points there, and goose's newest
#      applied row is its highest version, at least OpenFGA v1.22's minimum of 4 (the migrate Job, while it exists,
#      succeeded);
#   4. pg_hba: a role asking for the other side's database, a plaintext connection and the unused app role are all
#      rejected by pg_hba, before authentication; the control, a role asking for its own database over TLS, reaches
#      the password exchange. No password is sent: psql runs with -w and no PGPASSWORD or PGPASSFILE;
#   5. the NetworkPolicy: a pod in a third namespace, and a pod in an app namespace that is not OpenFGA, get no answer
#      on 5432, while the database pod itself (admitted) does;
#   6. each app namespace pulls the private image with ghcr-pull: the ServiceAccount and the Inkcast pod name it, and
#      the running pod's image id is the environment's digest.
#
#   ssh <lab> 'export PATH=/usr/local/bin:$PATH; eval $(crc oc-env); bash -s' < scripts/dev/shared-db-proof.sh
#
# Read-only except for the step-5 probe pods, which are deleted when they exit (oc run --rm). A database URI is
# printed with its password replaced; no other Secret value is read.
set -euo pipefail
k="${KUBECTL:-oc}"
db_ns=inkcast-db
probe_ns="${PROBE_NAMESPACE:-ldap-testing}"     # any namespace that is neither an app namespace nor inkcast-db
fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }

primary="$($k -n "$db_ns" get pods -l cnpg.io/cluster=inkcast-pg,cnpg.io/instanceRole=primary -o name)"
[ -n "$primary" ] || { echo "FAIL  no primary pod of inkcast-pg in $db_ns"; exit 1; }
image="$($k -n "$db_ns" get cluster inkcast-pg -o jsonpath='{.spec.imageName}')"
sql() { $k -n "$db_ns" exec "$primary" -c postgres -- psql -U postgres -v ON_ERROR_STOP=1 -tA "$@"; }

echo "== 1. the cluster"
phase="$($k -n "$db_ns" get cluster inkcast-pg -o jsonpath='{.status.phase}')"
if [ "$phase" = "Cluster in healthy state" ]; then pass "inkcast-pg: $phase"; else fail "inkcast-pg: $phase"; fi

echo "== 2. one database per side, owned by its role"
for side in staging prod; do
  owner="$(sql -c "select pg_get_userbyid(datdba) from pg_database where datname = 'openfga_$side'")"
  if [ "$owner" = "openfga_$side" ]; then pass "openfga_$side owned by $owner"; else fail "openfga_$side owned by '${owner:-<no such database>}'"; fi
done

echo "== 3. each environment migrated in its own database"
for side in staging prod; do
  ns="inkcast-$side"
  uri="$($k -n "$ns" get secret openfga-db-app -o jsonpath='{.data.uri}' | base64 -d | sed -E 's#//([^:/@]+):[^@]*@#//\1:<redacted>@#')"
  case "$uri" in
    "postgres://openfga_$side:<redacted>@inkcast-pg-rw.$db_ns.svc:5432/openfga_$side?sslmode=require") pass "$ns reads $uri" ;;
    *) fail "$ns reads $uri" ;;
  esac
  read -r newest highest <<<"$(sql -d "openfga_$side" -F ' ' -c \
    "select (select version_id from goose_db_version where is_applied order by id desc limit 1), max(version_id) from goose_db_version")"
  if [ "$newest" = "$highest" ] && [ "$highest" -ge 4 ]; then
    pass "openfga_$side at goose version $newest (newest applied row = highest version)"
  else
    fail "openfga_$side: newest applied row $newest, highest version $highest (OpenFGA v1.22 needs 4 or more)"
  fi
  if $k -n "$ns" get job openfga-migrate >/dev/null 2>&1; then
    ok="$($k -n "$ns" get job openfga-migrate -o jsonpath='{.status.succeeded}')"
    if [ "$ok" = 1 ]; then pass "$ns: migrate Job succeeded"; else fail "$ns: migrate Job succeeded='$ok'"; fi
  fi
done

echo "== 4. pg_hba, with no password sent"
connect() {  # user database sslmode -> psql's error line
  $k -n "$db_ns" exec "$primary" -c postgres -- env -u PGPASSWORD -u PGPASSFILE psql -w \
    "host=inkcast-pg-rw.$db_ns.svc port=5432 user=$1 dbname=$2 sslmode=$3 connect_timeout=5" -tAc 'select 1' 2>&1 || true
}
expect() {  # pattern user database sslmode
  out="$(connect "$2" "$3" "$4")"
  out="${out%%$'\n'*}"     # psql's line, not oc's "command terminated" after it
  case "$out" in
    *"$1"*) pass "user $2, database $3, sslmode $4: ${out##*FATAL:  }" ;;
    *) fail "user $2, database $3, sslmode $4: $out" ;;
  esac
}
expect 'pg_hba.conf rejects connection' openfga_staging openfga_prod require
expect 'pg_hba.conf rejects connection' openfga_prod openfga_staging require
expect 'pg_hba.conf rejects connection' openfga_staging openfga_staging disable
expect 'pg_hba.conf rejects connection' app app require
expect 'no password supplied' openfga_staging openfga_staging require
expect 'no password supplied' openfga_prod openfga_prod require

echo "== 5. the NetworkPolicy admits 5432 from OpenFGA's pods only"
overrides="$(printf '{"spec":{"securityContext":{"runAsNonRoot":true,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"probe","image":"%s","command":["pg_isready","-h","inkcast-pg-rw.%s.svc","-p","5432","-t","8"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}' "$image" "$db_ns")"
for ns in "$probe_ns" inkcast-staging; do
  out="$($k -n "$ns" run pg-reach-probe --rm -i --restart=Never --quiet --image="$image" \
    --labels=app.kubernetes.io/name=pg-reach-probe --overrides="$overrides" </dev/null 2>&1 || true)"   # under bash -s, stdin is this script
  case "$out" in
    *"no response"*) pass "a pod in $ns (not OpenFGA): no response on 5432" ;;
    *) fail "a pod in $ns (not OpenFGA): $out" ;;
  esac
done
out="$($k -n "$db_ns" exec "$primary" -c postgres -- pg_isready -h "inkcast-pg-rw.$db_ns.svc" -p 5432 -t 8 2>&1 || true)"
case "$out" in
  *"accepting connections"*) pass "control, the database pod (admitted): accepting connections" ;;
  *) fail "control, the database pod: $out" ;;
esac

echo "== 6. each app namespace pulls the image with ghcr-pull"
for ns in inkcast-staging inkcast-prod; do
  sa="$($k -n "$ns" get sa inkcast -o jsonpath='{.imagePullSecrets[*].name}')"
  digest="$($k -n "$ns" get deploy inkcast -o jsonpath='{.spec.template.spec.containers[?(@.name=="inkcast")].image}')"
  pod="$($k -n "$ns" get pods -l app.kubernetes.io/name=inkcast,app.kubernetes.io/component!=backup \
    -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.spec.imagePullSecrets[*].name} {.status.containerStatuses[0].imageID}{"\n"}{end}' | head -1)"
  case " $sa " in *" ghcr-pull "*) pass "$ns: ServiceAccount inkcast names ghcr-pull" ;; *) fail "$ns: ServiceAccount pull Secrets '$sa'" ;; esac
  if [ "$pod" = "ghcr-pull $digest" ]; then pass "$ns: running pod pulled $digest with ghcr-pull"
  else fail "$ns: expected 'ghcr-pull $digest', got '$pod'"; fi
done

echo "== result"
[ "$fails" -eq 0 ] && { echo "ALL PASS"; exit 0; }
echo "$fails FAILED"; exit 1
