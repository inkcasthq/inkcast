#!/bin/sh
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The lab's root Secret store, set up from inside the cluster (issue #436, ADR-0020; the Job in setup-job.yaml runs
# this with the store's own image: busybox sh, curl, weed). It writes a pair of Secrets holding one key pair, Velero's
# identity on the store:
#
#   inkcast-seaweedfs/seaweedfs-s3-identities   identities.json   what the store reads (-s3.config, deployment.yaml)
#   openshift-adp/cloud-credentials-root-secrets cloud            what Velero reads (shared/oadp/dpa.yaml)
#
# As `inkcast env-secrets` treats a pair: both present and equal, kept; one present, copied to the other; both
# absent, one key pair drawn from the kernel's CSPRNG and written to both; present and different, the run fails
# naming them and writes nothing. An existing Secret is never replaced (the account has no update or patch). Then
# it waits for the store and checks that the bucket exists (the server creates it at start: -bucket). Every value
# travels through this shell's variables, files in /tmp (memory, mode 600) and the API's TLS connection, never an
# argument (nothing in a process list) and never stdout or stderr: the output names Secrets and keys.
set -eu

sa=/var/run/secrets/kubernetes.io/serviceaccount
api="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT:-443}"
ns="$(cat "$sa/namespace")"
store=seaweedfs                                  # the Service beside this (service.yaml)
bucket=inkcast-root-secrets                      # declared on the server (deployment.yaml) and in the DPA
identity=velero                                  # the one identity the store knows
store_secret=seaweedfs-s3-identities
velero_ns=openshift-adp
velero_secret=cloud-credentials-root-secrets
work=/tmp/setup
umask 077
mkdir -p "$work"
printf 'Authorization: Bearer %s\n' "$(cat "$sa/token")" > "$work/auth"   # a header file: curl -H @file

say() { printf '%s\n' "$*"; }
fail() { printf 'seaweedfs-setup: %s\n' "$*" >&2; exit 1; }

# One call to the Secrets API: method, namespace, name ("" for the collection) and a body file ("" for none).
# Prints the status code; the body lands in $work/body.
call() {
  url="$api/api/v1/namespaces/$2/secrets${3:+/$3}"
  if [ -n "$4" ]; then
    curl -sS --cacert "$sa/ca.crt" -H "@$work/auth" -H 'Content-Type: application/json' -X "$1" \
      --data-binary "@$4" -o "$work/body" -w '%{http_code}' "$url"
  else
    curl -sS --cacert "$sa/ca.crt" -H "@$work/auth" -X "$1" -o "$work/body" -w '%{http_code}' "$url"
  fi
}

# The API's reason for a refusal, and nothing else of its answer: a message could quote an object.
reason() { sed -n 's/.*"reason": *"\([^"]*\)".*/\1/p' "$work/body" | head -n 1; }

# GET: leaves 200 or 404 in $code; anything else fails the run naming the Secret. Not a command substitution: `fail`
# must exit this shell, not a subshell that a caller would read as "absent".
read_secret() {
  code="$(call GET "$1" "$2" "")"
  case "$code" in
    200|404) ;;
    *) fail "read Secret $1/$2: HTTP $code $(reason). The Role that allows it is in deploy/kustomize/shared/seaweedfs (rbac.yaml) or shared/seaweedfs-access." ;;
  esac
}

# The decoded value of one data key of the Secret last read, or nothing when the key is absent.
data_key() { sed -n "s/.*\"$1\": *\"\([A-Za-z0-9+/=]*\)\".*/\1/p" "$work/body" | head -n 1 | base64 -d; }

# A key is 20 alphanumerics, a secret 40: what this script writes, and what the two files are parsed for.
valid() { [ "${#1}" -eq "$2" ] && case "$1" in *[!A-Za-z0-9]*|"") false ;; *) true ;; esac; }

# From the kernel's CSPRNG, letters and digits only, the length asked for.
random_alnum() {
  v=""
  while [ "${#v}" -lt "$1" ]; do
    v="$v$(head -c 96 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
  done
  printf '%s' "$v" | cut -c1-"$1"
}

# POST a Secret with one data key read from a file. 201: made. 409: another writer got there first, so the next
# run copies what it wrote. Anything else fails naming the Secret.
create_secret() {   # namespace name key file
  encoded="$(base64 < "$4" | tr -d '\n')"
  printf '{"apiVersion":"v1","kind":"Secret","type":"Opaque","metadata":{"name":"%s","labels":{"app.kubernetes.io/name":"seaweedfs","app.kubernetes.io/part-of":"inkcast","app.kubernetes.io/component":"root-secret-store"}},"data":{"%s":"%s"}}' \
    "$2" "$3" "$encoded" > "$work/manifest"
  code="$(call POST "$1" "" "$work/manifest")"
  rm -f "$work/manifest"
  case "$code" in
    201) say "$1/$2: created $3" ;;
    409) fail "create Secret $1/$2: it appeared while this ran (HTTP 409); the next run copies it." ;;
    *) fail "create Secret $1/$2: HTTP $code $(reason). The Role that allows it is in deploy/kustomize/shared/seaweedfs (rbac.yaml) or shared/seaweedfs-access." ;;
  esac
}

# 1. What each side holds.
store_key=""; store_secret_key=""; velero_key=""; velero_secret_key=""
read_secret "$ns" "$store_secret"
if [ "$code" = 200 ]; then
  data_key identities.json > "$work/identities.json"
  store_key="$(sed -n 's/.*"accessKey": *"\([A-Za-z0-9]*\)".*/\1/p' "$work/identities.json" | head -n 1)"
  store_secret_key="$(sed -n 's/.*"secretKey": *"\([A-Za-z0-9]*\)".*/\1/p' "$work/identities.json" | head -n 1)"
  if ! { valid "$store_key" 20 && valid "$store_secret_key" 40; }; then
    fail "$ns/$store_secret identities.json is not what this job writes (one identity with a 20- and a 40-character key): remove it, or write both Secrets by hand."
  fi
fi
read_secret "$velero_ns" "$velero_secret"
if [ "$code" = 200 ]; then
  data_key cloud > "$work/cloud"
  velero_key="$(sed -n 's/^aws_access_key_id=\([A-Za-z0-9]*\)$/\1/p' "$work/cloud" | head -n 1)"
  velero_secret_key="$(sed -n 's/^aws_secret_access_key=\([A-Za-z0-9]*\)$/\1/p' "$work/cloud" | head -n 1)"
  if ! { valid "$velero_key" 20 && valid "$velero_secret_key" 40; }; then
    fail "$velero_ns/$velero_secret cloud is not what this job writes (a [default] profile with a 20- and a 40-character key): remove it, or write both Secrets by hand."
  fi
fi

# 2. Decide, as env-secrets decides a pair.
if [ -n "$store_key" ] && [ -n "$velero_key" ]; then
  if [ "$store_key" = "$velero_key" ] && [ "$store_secret_key" = "$velero_secret_key" ]; then
    say "$ns/$store_secret and $velero_ns/$velero_secret: kept, in agreement"
  else
    fail "$ns/$store_secret and $velero_ns/$velero_secret disagree. Nothing was written: remove the one that is wrong (the store's, unless Velero has copies made with it) and run again; the next run copies the other."
  fi
else
  if [ -n "$store_key" ]; then
    key="$store_key"; secret="$store_secret_key"; origin="from $ns/$store_secret"
  elif [ -n "$velero_key" ]; then
    key="$velero_key"; secret="$velero_secret_key"; origin="from $velero_ns/$velero_secret"
  else
    key="$(random_alnum 20)"; secret="$(random_alnum 40)"; origin="generated"
  fi
  if [ -z "$store_key" ]; then
    printf '{"identities": [{"name": "%s", "credentials": [{"accessKey": "%s", "secretKey": "%s"}], "actions": ["Read:%s", "Write:%s", "List:%s"]}]}\n' \
      "$identity" "$key" "$secret" "$bucket" "$bucket" "$bucket" > "$work/identities.json"
    create_secret "$ns" "$store_secret" identities.json "$work/identities.json"
    say "  identity $identity on bucket $bucket: Read, Write, List ($origin)"
  fi
  if [ -z "$velero_key" ]; then
    printf '[default]\naws_access_key_id=%s\naws_secret_access_key=%s\n' "$key" "$secret" > "$work/cloud"
    create_secret "$velero_ns" "$velero_secret" cloud "$work/cloud"
    say "  the storage location root-secrets' credential ($origin)"
  fi
fi
rm -rf "$work"

# 3. The store lists the bucket the server declares. The listing itself is the wait: `mini` starts the master before
# the filer, and the pod may be starting on the identities file this run just wrote, so the shell is asked again
# until it answers (five minutes), then the answer is judged once.
i=0
while :; do
  listing="$(printf 's3.bucket.list\n' | weed shell -master="$store:9333" -filer="$store:8888" 2>&1)" && break
  i=$((i + 1))
  [ "$i" -le 60 ] || fail "the store did not list its buckets within five minutes (oc -n $ns logs deploy/seaweedfs). weed shell said: $listing"
  sleep 5
done
case "$listing" in
  *"$bucket"*) say "bucket $bucket: present" ;;
  *) fail "bucket $bucket is absent on the store (the server creates it at start: deployment.yaml, -bucket). weed shell said: $listing" ;;
esac
say "$ns: the store, its identity and the storage location's credential are in place."
