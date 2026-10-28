#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The cluster makes every generated Secret of a lab environment, on kind (E3.F3.9.S7, issue #323): the shared database
# with its Roles (overlays/ci-inkcast-db), the Keycloak access (shared/keycloak-access), and an environment composed
# as environments/staging composes it (overlays/ci-env-secrets: OpenFGA on the shared database, components/
# env-secrets, no Secret made by anyone). Each proof fails the script:
#
#   1. fresh: none of the four Secrets exists; one apply; OpenFGA (its migrate Job against the shared database with
#      the generated password, the server with the generated preshared key) and Inkcast (the mounted inkcast-secrets)
#      come up; the three pairs agree; the init container's log holds no value;
#   2. the namespace rebuilt (deleted, applied again): the role's password and both client secrets come back the
#      same, copied from inkcast-db and keycloak, and OpenFGA connects with them;
#   3. the shared sides lost: the next start of OpenFGA's pod copies them back from the app namespace;
#   4. a disagreement: the new pod's init container fails naming the pair, with no value in its log, and the previous
#      pod keeps serving; once the wrong key is removed, the kubelet's own retry finishes the rollout, with no apply;
#   5. both sides of the role's password lost: the next pod start makes a new pair and that same pod's OpenFGA
#      connects with it, with no restart after.
#
#   scripts/dev/env-secrets-e2e.sh    # after scripts/dev/openfga-e2e.sh (it installs the CloudNativePG operator),
#                                     # on a kind cluster with the inkcast:ci image loaded, as in the kind CI job
#
# No value is ever printed: the checks read the Secrets in Python and print names, lengths and verdicts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
k="${KUBECTL:-kubectl}"
ns=inkcast-staging
work="$(mktemp -d)"
PIDS=()
stop_forwards() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; PIDS=(); }
cleanup() { stop_forwards; rm -rf "$work"; }
trap cleanup EXIT
forward() {  # service local:remote; background, stopped by stop_forwards
  $k -n "$ns" port-forward "svc/$1" "$2" >/dev/null 2>&1 &
  PIDS+=("$!")
}

cat > "$work/check.py" <<'PY'
"""The environment's four Secrets, read with kubectl and checked here: only names, lengths and verdicts are printed."""
import base64, hashlib, json, os, string, subprocess, sys

NS = "inkcast-staging"
REFS = {"role": ("inkcast-db", "inkcast-pg-openfga-staging"), "app-db": (NS, "openfga-db-app"),
        "realm": ("keycloak", "inkcast-staging-realm"), "app": (NS, "inkcast-secrets")}
URI = "postgres://openfga_staging:{}@inkcast-pg-rw.inkcast-db.svc:5432/openfga_staging?sslmode=require"
ALNUM = set(string.ascii_letters + string.digits)


def read(which):
    ns, name = REFS[which]
    done = subprocess.run(["kubectl", "-n", ns, "get", "secret", name, "-o", "json", "--ignore-not-found"],
                          capture_output=True, text=True, check=True)
    if not done.stdout.strip():
        return None
    obj = json.loads(done.stdout)
    return {"type": obj["type"], "labels": obj["metadata"].get("labels", {}),
            "data": {k: base64.b64decode(v).decode() for k, v in obj.get("data", {}).items()}}


def fail(message):
    sys.exit(f"FAIL: {message}")


def pairs():
    role, app_db, realm, app = (read(w)["data"] for w in ("role", "app-db", "realm", "app"))
    return {"password": (role["password"], app_db["password"]),
            "identity": (realm["identity-client-secret"], app["oidc-client-secret"]),
            "sync": (realm["sync-client-secret"], app["keycloak-sync-secret"])}


def fingerprints():
    return {name: hashlib.sha256(a.encode()).hexdigest() for name, (a, _) in pairs().items()}


mode, args = sys.argv[1], sys.argv[2:]
if mode == "absent":
    for which, (ns, name) in REFS.items():
        if read(which) is not None:
            fail(f"{ns}/{name} exists before the environment is applied")
        print(f"absent: {ns}/{name}")
elif mode == "agree":
    found = {w: read(w) for w in REFS}
    for which, obj in found.items():
        if obj is None:
            fail(f"{'/'.join(REFS[which])} is missing")
    role, app_db, realm, app = (found[w]["data"] for w in ("role", "app-db", "realm", "app"))
    if found["role"]["type"] != "kubernetes.io/basic-auth" or found["role"]["labels"].get("cnpg.io/reload") != "true":
        fail("the role Secret is not basic-auth with cnpg.io/reload")
    if role["username"] != "openfga_staging" or app_db["username"] != "openfga_staging":
        fail("a username is not openfga_staging")
    for name, (a, b) in pairs().items():
        if a != b:
            fail(f"the {name} pair disagrees")
        print(f"agree: {name} ({len(a)} characters on both sides)")
    if app_db["uri"] != URI.format(role["password"]):
        fail("openfga-db-app's uri is not the shared database's with the role's password and sslmode=require")
    if len(role["password"]) != 48 or set(role["password"]) - set("0123456789abcdef"):
        fail("the role's password is not 48 hex characters")
    for key in ("identity-client-secret", "sync-client-secret"):
        if set(realm[key]) - ALNUM:
            fail(f"{key} holds a character outside A-Z a-z 0-9")
    if len(app["secret-key"]) < 32 or set(app["fga-api-token"]) - ALNUM:
        fail("secret-key or fga-api-token is not what its consumer needs")
    print("agree: uri, types, labels, alphabets")
elif mode == "fingerprint":
    with open(args[0], "w") as f:
        json.dump(fingerprints(), f)
    print("recorded the pairs' fingerprints")
elif mode == "same":
    before, now = json.load(open(args[0])), fingerprints()
    for name in args[1:]:
        if before[name] != now[name]:
            fail(f"the {name} pair changed")
        print(f"same as before: {name}")
elif mode == "changed":
    before, now = json.load(open(args[0])), fingerprints()
    for name in args[1:]:
        if before[name] == now[name]:
            fail(f"the {name} pair did not change")
        print(f"new: {name}")
elif mode == "no-value":
    text = open(args[0]).read()
    found = [read(w) for w in REFS]
    values = [v for obj in found if obj for k, v in obj["data"].items() if k != "username" and len(v) >= 8]
    if any(v in text for v in values):
        fail(f"{args[0]} holds a Secret's value")
    print(f"no value in the log ({len(text.splitlines())} lines, {len(values)} values checked)")
elif mode == "header":
    fd = os.open(args[0], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"Authorization: Bearer {read('app')['data']['fga-api-token']}\n")
else:
    fail(f"unknown mode {mode}")
PY
check() { python3 "$work/check.py" "$@"; }

up() {  # the environment applied and serving: OpenFGA on the shared database, its migrate Job, Inkcast
  $k apply -k "$ROOT/deploy/kustomize/overlays/ci-env-secrets"
  $k -n "$ns" rollout status deploy/openfga --timeout=600s
  $k -n "$ns" wait --for=condition=complete job/openfga-migrate --timeout=600s
  $k -n "$ns" rollout status deploy/inkcast --timeout=300s
}
serving() {  # OpenFGA answers with the key from inkcast-secrets and refuses without it; Inkcast is ready on its files
  check header "$work/auth-header"
  forward openfga 28180:8080
  forward inkcast-metrics 29190:9090
  for _ in $(seq 1 30); do curl -sf localhost:28180/healthz >/dev/null && curl -sf localhost:29190/readyz >/dev/null && break; sleep 1; done
  code="$(curl -s -o /dev/null -w '%{http_code}' -H @"$work/auth-header" localhost:28180/stores)"
  [ "$code" = 200 ] || { echo "OpenFGA answered $code to the generated key" >&2; exit 1; }
  code="$(curl -s -o /dev/null -w '%{http_code}' localhost:28180/stores)"
  [ "$code" = 401 ] || { echo "OpenFGA answered $code without a key" >&2; exit 1; }
  echo "OpenFGA: 200 with the key from inkcast-secrets, 401 without"
  curl -sf localhost:29190/readyz | python3 -c 'import json,sys; b=json.load(sys.stdin); print("Inkcast /readyz:", b.get("ok"), b.get("problems")); sys.exit(0 if b.get("ok") is True else 1)'
  curl -sf localhost:29190/secrets | python3 -c '
import json, sys
found = {s["name"]: s for s in json.load(sys.stdin)["secrets"]}
for name in ("INKCAST_SECRET_KEY", "OIDC_CLIENT_SECRET"):
    s = found.get(name) or {}
    print(name, {k: s.get(k) for k in ("present", "source")})
    if not (s.get("present") and str(s.get("source", "")).startswith("file:")):
        sys.exit(f"{name} was not read from the mounted inkcast-secrets")'
  stop_forwards
}
init_log() {  # the env-secrets container's log of the newest OpenFGA pod, checked for values
  pod="$($k -n "$ns" get pods -l app.kubernetes.io/name=openfga,app.kubernetes.io/component=authz \
    --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')"
  $k -n "$ns" logs "$pod" -c env-secrets > "$work/init.log"
  cat "$work/init.log"
  check no-value "$work/init.log"
}

echo "== Prerequisites: the CloudNativePG operator (scripts/dev/openfga-e2e.sh installs it), Keycloak's namespace"
$k -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=120s
$k get namespace keycloak -o name >/dev/null 2>&1 || $k create namespace keycloak
echo "== The shared database and the Keycloak access, as the lab applies them"
$k apply -k "$ROOT/deploy/kustomize/overlays/ci-inkcast-db"
$k apply -k "$ROOT/deploy/kustomize/shared/keycloak-access"
$k -n inkcast-db wait --for=condition=Ready cluster/inkcast-pg --timeout=600s

echo "-- 1. a fresh environment: no Secret, one apply"
check absent
up
check agree
check fingerprint "$work/first"
init_log
serving

echo "-- 2. the namespace rebuilt: the values come back from inkcast-db and keycloak"
$k delete namespace "$ns" --timeout=300s
up
check agree
check same "$work/first" password identity sync
init_log
serving

echo "-- 3. the shared sides lost: the next start of OpenFGA's pod copies them back"
$k -n inkcast-db delete secret inkcast-pg-openfga-staging
$k -n keycloak delete secret inkcast-staging-realm
$k -n "$ns" rollout restart deploy/openfga
$k -n "$ns" rollout status deploy/openfga --timeout=600s
check agree
check same "$work/first" password identity sync
init_log

echo "-- 4. a disagreement stops the new pod and names the pair; removing the wrong key lets the kubelet finish it"
# A test value, not a secret: the realm side now disagrees with inkcast-secrets.
$k -n keycloak patch secret inkcast-staging-realm --type merge \
  -p "{\"data\":{\"identity-client-secret\":\"$(printf 'disagrees-on-purpose-0123456789' | base64)\"}}"
$k -n "$ns" rollout restart deploy/openfga
failed=""
for _ in $(seq 1 60); do
  failed="$($k -n "$ns" get pods -l app.kubernetes.io/name=openfga,app.kubernetes.io/component=authz -o json | python3 -c '
import json, sys
for pod in json.load(sys.stdin)["items"]:
    for s in pod["status"].get("initContainerStatuses", []):
        ended = s.get("state", {}).get("terminated") or s.get("lastState", {}).get("terminated") or {}
        if s["name"] == "env-secrets" and ended.get("exitCode") == 1:
            print(pod["metadata"]["name"])')"
  [ -n "$failed" ] && break
  sleep 2
done
[ -n "$failed" ] || { echo "the init container did not fail on the disagreement within 120 s" >&2; exit 1; }
$k -n "$ns" logs "$failed" -c env-secrets > "$work/init.log" 2>/dev/null \
  || $k -n "$ns" logs "$failed" -c env-secrets --previous > "$work/init.log"
cat "$work/init.log"
check no-value "$work/init.log"
named="$(cat "$work/init.log")"
case "$named" in
  *"keycloak/inkcast-staging-realm identity-client-secret and inkcast-staging/inkcast-secrets oidc-client-secret disagree"*) ;;
  *) echo "the init container's log does not name the pair" >&2; exit 1 ;;
esac
case "$named" in *disagrees-on-purpose*) echo "the log holds the test value" >&2; exit 1 ;; esac
available="$($k -n "$ns" get deploy openfga -o jsonpath='{.status.availableReplicas}')"
[ "$available" = 1 ] || { echo "OpenFGA has $available available replicas while the new pod is held" >&2; exit 1; }
echo "the previous pod keeps serving ($available available) while the new one is held"
$k -n keycloak patch secret inkcast-staging-realm --type json -p '[{"op":"remove","path":"/data/identity-client-secret"}]'
$k -n "$ns" rollout status deploy/openfga --timeout=600s   # no apply: the kubelet's retry of the init container
check agree
check same "$work/first" password identity sync

echo "-- 5. both sides of the role's password lost: a new pair, and the pod that made it connects with it"
$k -n inkcast-db delete secret inkcast-pg-openfga-staging
$k -n "$ns" delete secret openfga-db-app
$k -n "$ns" rollout restart deploy/openfga
$k -n "$ns" rollout status deploy/openfga --timeout=600s
check agree
check changed "$work/first" password
check same "$work/first" identity sync
init_log
serving

$k delete namespace "$ns" inkcast-db --wait=false   # frees the node for the next step of the kind job
echo "PASS: every generated Secret made by the cluster, in agreement, copied back on a rebuild, refused on a disagreement"
