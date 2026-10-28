#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# One environment's credentials for the lab's shared PostgreSQL (deploy/kustomize/shared/inkcast-db, issue #296):
# a password for the role openfga_<side>, generated once and written as two Secrets.
#
#   scripts/deploy/openfga-db-credentials.sh <staging|prod>
#
#   inkcast-db/inkcast-pg-openfga-<side>   kubernetes.io/basic-auth (username, password): the role's password, which
#                                          the Cluster's managed role reads (cluster.yaml; labelled cnpg.io/reload so
#                                          the operator picks it up at once).
#   inkcast-<side>/openfga-db-app          uri, username, password: what OpenFGA's Deployment and migrate Job read
#                                          (components/openfga/deployment.yaml, job.yaml), pointing at the shared
#                                          cluster: postgres://<role>:<password>@inkcast-pg-rw.inkcast-db.svc:5432/
#                                          openfga_<side>?sslmode=require.
#
# The password is 48 hex characters from `openssl rand`, so it needs no percent-encoding in the URI. It goes from
# this shell to `oc create -f -` on stdin, never in an argument (nothing in `ps`), never on stdout or stderr. A pair
# that exists is kept, never replaced (the role's password is the database's; replacing one side would cut OpenFGA
# off): a second run reports it and exits 0. When only one Secret of the pair exists, or the two disagree, the
# script fails naming the Secret and changes nothing: delete both to generate the pair again, or write the missing
# side by hand from the one that exists. Passwords, by the owner's decision (cert-manager client certificates
# instead: #299).
#
# sslmode=require: the connection is always TLS (the server admits the role over TLS only, cluster.yaml's pg_hba),
# with the server's certificate not verified; OpenFGA's driver, pgx, reads `require` as libpq does (pgconn/config.go
# at v5.11.0, the version OpenFGA v1.22.0 pins). verify-full would need the cluster's CA (Secret inkcast-pg-ca in
# inkcast-db) copied into the app namespace and mounted on OpenFGA's pods, a change to the component that belongs
# with #299. The operator's own `uri` carries no sslmode at all, so `require` is the stricter of the two.
set -euo pipefail
set +x  # never traced (bash -x, SHELLOPTS=xtrace): the trace would print the password
side="${1:-}"
case "$side" in
  staging|prod) ;;
  *) echo "usage: openfga-db-credentials.sh <staging|prod>" >&2; exit 2 ;;
esac
db_ns=inkcast-db
app_ns="inkcast-$side"
role="openfga_$side"
database="openfga_$side"
role_secret="inkcast-pg-openfga-$side"
app_secret=openfga-db-app
host=inkcast-pg-rw.inkcast-db.svc
port=5432

# Both namespaces first, each with the command that creates it: nothing is written until both are there, so a
# missing app namespace never leaves half a pair behind. Any other failure of oc (no cluster, no access) is its own.
namespace_exists() {  # namespace, and what creates it
  local answer
  if answer="$(oc get namespace "$1" -o name 2>&1)"; then return 0; fi
  case "$answer" in
    *NotFound*) echo "Namespace $1 does not exist: $2 first." >&2 ;;
    *) echo "$answer" >&2 ;;
  esac
  exit 1
}
namespace_exists "$db_ns" "oc apply -k deploy/kustomize/shared/inkcast-db"
namespace_exists "$app_ns" "oc new-project $app_ns"

# Any failure of oc in these two stops the script with oc's own message (an explicit exit: errexit does not reach a
# function called in an && list or a $(...) test), so an outage mid-run never reads as "absent" or "empty".
secret_exists() {  # namespace name: true when the Secret is there
  local found
  found="$(oc -n "$1" get secret "$2" --ignore-not-found -o name)" || exit 1
  [ -n "$found" ]
}
secret_key() {  # namespace name key: the key's value decoded, empty when the Secret has no such key
  local encoded
  encoded="$(oc -n "$1" get secret "$2" --ignore-not-found -o "jsonpath={.data.$3}")" || exit 1
  [ -n "$encoded" ] || return 0
  printf '%s' "$encoded" | base64 -d
}
uri_for() {  # password: the URI OpenFGA reads, built the one way so an existing one can be compared whole
  printf 'postgres://%s:%s@%s:%s/%s?sslmode=require' "$role" "$1" "$host" "$port" "$database"
}

role_present=false; app_present=false
secret_exists "$db_ns" "$role_secret" && role_present=true
secret_exists "$app_ns" "$app_secret" && app_present=true

if $role_present && $app_present; then
  # Kept, once both sides agree: the same password, the role Secret's username is the role, and the app Secret's
  # uri is exactly what this script would write (role, host, port, database, sslmode). Each value is read in an
  # assignment, where errexit stops the script on an oc failure; read inside a test, the failure would compare as empty.
  role_pw="$(secret_key "$db_ns" "$role_secret" password)"
  role_user="$(secret_key "$db_ns" "$role_secret" username)"
  if [ -z "$role_pw" ] || [ "$role_user" != "$role" ]; then
    echo "Secret $db_ns/$role_secret is not a basic-auth Secret for role $role (username and password): nothing changed." >&2
    exit 1
  fi
  app_pw="$(secret_key "$app_ns" "$app_secret" password)"
  app_user="$(secret_key "$app_ns" "$app_secret" username)"
  app_uri="$(secret_key "$app_ns" "$app_secret" uri)"
  if [ "$app_pw" != "$role_pw" ] || [ "$app_user" != "$role" ] || [ "$app_uri" != "$(uri_for "$role_pw")" ]; then
    echo "Secret $app_ns/$app_secret disagrees with $db_ns/$role_secret (the password, or the uri's role, host, port," \
         "database or sslmode): nothing changed. Delete both to generate the pair again, or write $app_ns/$app_secret" \
         "by hand from $db_ns/$role_secret." >&2
    exit 1
  fi
  echo "Kept Secret $db_ns/$role_secret and $app_ns/$app_secret: they agree (role $role, $host:$port/$database, sslmode=require)."
  exit 0
fi
if $role_present; then
  echo "Secret $db_ns/$role_secret exists but $app_ns/$app_secret does not: nothing changed. Delete $db_ns/$role_secret" \
       "to generate the pair again, or write $app_ns/$app_secret by hand from it (uri, username, password)." >&2
  exit 1
fi
if $app_present; then
  echo "Secret $app_ns/$app_secret exists but $db_ns/$role_secret does not: nothing changed. Delete $app_ns/$app_secret" \
       "to generate the pair again, or write $db_ns/$role_secret by hand from it (basic-auth: username, password)." >&2
  exit 1
fi

# Neither side: one password for both. The values are quoted in the YAML because a hex string can read as a number.
password="$(openssl rand -hex 24)"
printf '%s\n' \
  'apiVersion: v1' \
  'kind: Secret' \
  'type: kubernetes.io/basic-auth' \
  'metadata:' \
  "  name: $role_secret" \
  '  labels:' \
  '    app.kubernetes.io/part-of: inkcast' \
  '    cnpg.io/reload: "true"' \
  'stringData:' \
  "  username: \"$role\"" \
  "  password: \"$password\"" \
  | oc -n "$db_ns" create -f - >/dev/null
printf '%s\n' \
  'apiVersion: v1' \
  'kind: Secret' \
  'type: Opaque' \
  'metadata:' \
  "  name: $app_secret" \
  '  labels:' \
  '    app.kubernetes.io/part-of: inkcast' \
  'stringData:' \
  "  username: \"$role\"" \
  "  password: \"$password\"" \
  "  uri: \"$(uri_for "$password")\"" \
  | oc -n "$app_ns" create -f - >/dev/null
echo "Created Secret $db_ns/$role_secret (kubernetes.io/basic-auth, role $role) and $app_ns/$app_secret (uri, username," \
     "password): $role on $host:$port/$database, sslmode=require."
