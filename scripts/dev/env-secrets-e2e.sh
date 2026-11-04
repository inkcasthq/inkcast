#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The cluster makes every generated Secret of a lab environment, on kind (E3.F3.9.S7, issue #323): the admission policy
# (shared/env-secrets-admission), the shared database with its Roles (overlays/ci-inkcast-db), the Keycloak access
# (shared/keycloak-access), and an environment composed
# as environments/staging composes it (overlays/ci-env-secrets: OpenFGA on the shared database, components/
# env-secrets, no Secret made by anyone). Each proof fails the script:
#
#   1. fresh: none of the four Secrets exists; one apply; OpenFGA (its init container migrate against the shared
#      database with the generated password, the server with the generated preshared key) and Inkcast (the mounted
#      inkcast-secrets) come up; the three pairs agree; the init container's log holds no value;
#  1b. staging's account is denied a create of production's Secrets, or of any other name, in inkcast-db and keycloak
#      (a server-side dry run, as that account), and admitted for its own;
#   2. the namespace rebuilt (deleted, applied again): the role's password and both client secrets come back the
#      same, copied from inkcast-db and keycloak, and OpenFGA connects with them;
#   3. the shared sides lost: the next start of OpenFGA's pod copies them back from the app namespace;
#   4. a disagreement: the new pod's init container fails naming the pair, with no value in its log; OpenFGA's
#      Deployment is `strategy: Recreate` (PR #374), so the previous pod is already gone and OpenFGA is unavailable
#      while the new pod is held; once the wrong key is removed, the kubelet's own retry finishes the rollout, with
#      no apply;
#   5. both sides of the role's password lost: the next pod start makes a new pair and that same pod's OpenFGA
#      connects with it, with no restart after;
#   6. the role Secret deleted, with no restart and no apply (#342): a run of the CronJob inkcast-env-secrets, made
#      from its own template, brings it back copied from openfga-db-app, the pair agrees, OpenFGA's pod is untouched
#      and the run's log holds no value; and the CronJob the cluster runs (schedule, concurrency policy, starting
#      deadline) is the one environments/staging and environments/production render (checked at each `up`, before
#      the CronJob is suspended for the rest of the script);
#   7. inkcast-secrets deleted (#423, the owner's decision): the CronJob's run (--repair-only) fails naming its two
#      lost values and the runbook, makes nothing, prints no value, OpenFGA's pod and the other Secrets are untouched
#      and OpenFGA still answers with the key it has; the Secret is then put back from the copy saved before, as a
#      restore would.
#
# The live CronJob is suspended right after each apply (review C2 of PR #423): a real tick landing during proofs 3,
# 5, 6 or 7 would copy a Secret back before the proof does, and the controller prunes Jobs beyond
# successfulJobsHistoryLimit (1), the manual ones included. A suspended CronJob's `kubectl create job --from` still
# runs (measured in that review), which is what proofs 6 and 7 use. The lab's CronJob is never suspended.
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

cronjob_spec() {  # target: the schedule, concurrency policy and starting deadline of the CronJob it renders
  rm -rf "$work/rendered" && mkdir "$work/rendered"   # an existing directory: one file per object (a path that is not one gets one file)
  $k kustomize "$ROOT/deploy/kustomize/$1" -o "$work/rendered"
  $k create --dry-run=client -f "$work/rendered/batch_v1_cronjob_inkcast-env-secrets.yaml" \
    -o jsonpath='{.spec.schedule} {.spec.concurrencyPolicy} {.spec.startingDeadlineSeconds}'
}
up() {  # the environment applied and serving: OpenFGA on the shared database (env-secrets, then migrate, in its pod), Inkcast
  $k apply -k "$ROOT/deploy/kustomize/overlays/ci-env-secrets"
  $k -n "$ns" rollout status deploy/openfga --timeout=600s
  $k -n "$ns" rollout status deploy/inkcast --timeout=300s
  # The CronJob the cluster runs is the one both environments render (#342): checked here, on the object the apply
  # made and before it is suspended below, since `suspend` is the one field the e2e sets that the environments don't.
  live="$($k -n "$ns" get cronjob inkcast-env-secrets -o jsonpath='{.spec.schedule} {.spec.concurrencyPolicy} {.spec.startingDeadlineSeconds}')"
  for target in environments/staging environments/production; do
    rendered="$(cronjob_spec "$target")"
    [ "$rendered" = "$live" ] || { echo "$target renders the CronJob as '$rendered'; the cluster runs '$live'" >&2; exit 1; }
  done
  echo "the CronJob runs '$live' (schedule, concurrency policy, starting deadline), as both environments render it"
  # Suspended for the rest of the script (review C2 of PR #423): a tick during proofs 3, 5, 6 or 7 would copy a
  # Secret back before the proof does, and the controller prunes Jobs beyond successfulJobsHistoryLimit, the manual
  # ones too. `kubectl create job --from` still runs from a suspended CronJob: proofs 6 and 7 rely on that.
  $k -n "$ns" patch cronjob inkcast-env-secrets -p '{"spec":{"suspend":true}}'
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
echo "== The admission policy, the shared database and the Keycloak access, as the lab applies them"
$k apply -k "$ROOT/deploy/kustomize/shared/env-secrets-admission"
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

echo "-- 1b. staging's account cannot create production's Secrets (the admission policy; RBAC lets it create any name)"
as_staging=(--as=system:serviceaccount:inkcast-staging:inkcast-env-secrets --dry-run=server -o name)
denied() {  # namespace name: a server-side dry run as staging's account, which admission runs and nothing stores
  local said
  for _ in $(seq 1 15); do   # a new policy takes effect within seconds of its apply
    said="$($k -n "$1" create secret generic "$2" --from-literal=probe=x "${as_staging[@]}" 2>&1)" || true
    case "$said" in *"creates only its own Secrets, not $1/$2"*) echo "denied: $1/$2"; return 0 ;; esac
    sleep 2
  done
  echo "staging's account was not denied $1/$2: $said" >&2; exit 1
}
denied inkcast-db inkcast-pg-openfga-prod
denied keycloak inkcast-prod-realm
denied keycloak a-secret-of-any-other-name
# Its own: admission lets it through, and the store answers that it exists.
own="$($k -n keycloak create secret generic inkcast-staging-realm --from-literal=probe=x "${as_staging[@]}" 2>&1)" || true
case "$own" in *AlreadyExists*|*"already exists"*) echo "admitted: keycloak/inkcast-staging-realm (it exists)" ;;
  *) echo "staging's own Secret was not admitted: $own" >&2; exit 1 ;; esac

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
        if s["name"] != "env-secrets":
            continue
        # The attempt that failed: the current one, or the previous one once the kubelet has started its retry,
        # whose own log is empty until it fails too. The kubelet can still start the retry before the log is read:
        # the read below then falls back to --previous (#387).
        if s.get("state", {}).get("terminated", {}).get("exitCode") == 1:
            print(pod["metadata"]["name"])
        elif s.get("lastState", {}).get("terminated", {}).get("exitCode") == 1:
            print(pod["metadata"]["name"], "--previous")')"
  [ -n "$failed" ] && break
  sleep 2
done
[ -n "$failed" ] || { echo "the init container did not fail on the disagreement within 120 s" >&2; exit 1; }
# shellcheck disable=SC2086  # "$failed" is the pod's name, then --previous when the failed attempt is the last one
$k -n "$ns" logs $failed -c env-secrets > "$work/init.log"
pair="keycloak/inkcast-staging-realm identity-client-secret and inkcast-staging/inkcast-secrets oidc-client-secret disagree"
if [ "$failed" = "${failed%% *}" ] && ! grep -qF "$pair" "$work/init.log"; then
  # The retry started between the check and the read, so the log read was the new attempt's, not yet failed. The
  # attempt that failed is now the previous one; every attempt fails on the same disagreement (#387).
  $k -n "$ns" logs "$failed" -c env-secrets --previous > "$work/init.log"
fi
cat "$work/init.log"
check no-value "$work/init.log"
named="$(cat "$work/init.log")"
case "$named" in
  *"$pair"*) ;;
  *) echo "the init container's log does not name the pair" >&2; exit 1 ;;
esac
case "$named" in *disagrees-on-purpose*) echo "the log holds the test value" >&2; exit 1 ;; esac
# `strategy: Recreate` (components/openfga/deployment.yaml, the owner's decision on PR #374) ends the previous pod
# before the new one starts, so a held new pod is an outage until the wrong key is removed: the cost of Recreate,
# stated here rather than hidden.
available="$($k -n "$ns" get deploy openfga -o jsonpath='{.status.availableReplicas}')"
[ "${available:-0}" = 0 ] || { echo "OpenFGA has $available available replicas while the new pod is held: Recreate should have ended the previous pod" >&2; exit 1; }
echo "the previous pod is gone (Recreate) and OpenFGA is unavailable (${available:-0} available) while the new one is held"
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

echo "-- 6. the role Secret deleted, no restart and no apply: a run of the CronJob brings it back (#342)"
# The CronJob inkcast-env-secrets (components/env-secrets/cronjob.yaml) runs `inkcast env-secrets` every ten
# minutes. CI does not wait for a tick: a Job made from the CronJob's own template (`kubectl create job --from`)
# runs exactly the pod spec the controller creates at a tick (the same account, token, image and security context),
# so the run is proven on the cluster; the tick is a manifest fact, proven by comparing what the cluster runs with
# what both environments render, so what CI runs here is what the lab gets.
openfga_pod() {  # OpenFGA's pod and its server container's restart count: a restart or a rollout changes one of them
  $k -n "$ns" get pods -l app.kubernetes.io/name=openfga,app.kubernetes.io/component=authz \
    -o jsonpath='{.items[*].metadata.name}/{.items[*].status.containerStatuses[0].restartCount}'
}
job_logs() {  # job: the log of every pod the Job made (every attempt, not only the one that succeeded), checked for values
  : > "$work/job.log"
  for pod in $($k -n "$ns" get pods -l "batch.kubernetes.io/job-name=$1" --sort-by=.metadata.creationTimestamp -o name); do
    echo "--- $pod" >> "$work/job.log"
    $k -n "$ns" logs "$pod" >> "$work/job.log" 2>&1 || true
  done
  cat "$work/job.log"
  check no-value "$work/job.log"
}
check fingerprint "$work/fifth"
before="$(openfga_pod)"
$k -n inkcast-db delete secret inkcast-pg-openfga-staging
$k -n "$ns" create job env-secrets-e2e --from=cronjob/inkcast-env-secrets
$k -n "$ns" wait --for=condition=complete job/env-secrets-e2e --timeout=180s \
  || { $k -n "$ns" describe job env-secrets-e2e; echo "the run did not complete within 180 s" >&2; exit 1; }
job_logs env-secrets-e2e
case "$(cat "$work/job.log")" in
  *"inkcast-db/inkcast-pg-openfga-staging: created"*) ;;
  *) echo "the run's log does not say it created inkcast-db/inkcast-pg-openfga-staging" >&2; exit 1 ;;
esac
check agree
check same "$work/fifth" password identity sync
after="$(openfga_pod)"
[ "$before" = "$after" ] || { echo "OpenFGA's pod changed ($before -> $after): the Secret should be back with no restart" >&2; exit 1; }
echo "back within one run, copied from openfga-db-app, with no apply; OpenFGA's pod untouched ($after)"
$k -n "$ns" delete job env-secrets-e2e

echo "-- 7. inkcast-secrets deleted: the CronJob's run fails naming its two lost values and makes nothing (#423)"
# --repair-only (the owner's decision on PR #423): secret-key and fga-api-token are held only in inkcast-secrets, so
# deleting it loses them; a run that made new ones would cut the running pods off (every sealed credential
# unreadable, the new fga-api-token refused by the running OpenFGA until it restarts). The run refuses, exits 1 so
# the Job is Failed, and names what is lost and the runbook. The Secret is saved first (a 0600 file, never printed)
# and put back whole afterwards, as the runbook's restore does, so the environment is as it was.
(umask 077; $k -n "$ns" get secret inkcast-secrets -o json | python3 -c '
import json, sys
obj = json.load(sys.stdin)
json.dump({"apiVersion": "v1", "kind": "Secret", "type": obj["type"], "data": obj["data"],
           "metadata": {"name": obj["metadata"]["name"], "labels": obj["metadata"].get("labels", {})}}, sys.stdout)' \
  > "$work/inkcast-secrets.json")
before="$(openfga_pod)"
$k -n "$ns" delete secret inkcast-secrets
$k -n "$ns" create job env-secrets-e2e-lost --from=cronjob/inkcast-env-secrets
$k -n "$ns" wait --for=condition=Failed job/env-secrets-e2e-lost --timeout=300s \
  || { $k -n "$ns" describe job env-secrets-e2e-lost; echo "the run did not fail within 300 s" >&2; exit 1; }
if $k -n "$ns" get secret inkcast-secrets -o name >/dev/null 2>&1; then
  echo "the run made a new inkcast-secrets: it must make no new value" >&2; exit 1
fi
job_logs env-secrets-e2e-lost
lost="inkcast-staging/inkcast-secrets is absent: secret-key and fga-api-token are held nowhere else and lost"
case "$(cat "$work/job.log")" in
  *"$lost"*) ;;
  *) echo "the run's log does not name the lost values" >&2; exit 1 ;;
esac
case "$(cat "$work/job.log")" in
  *"docs/runbooks/env-secrets.md"*) ;;
  *) echo "the run's log does not name the runbook" >&2; exit 1 ;;
esac
python3 - "$work/inkcast-secrets.json" "$work/job.log" <<'PY'
"""The deleted Secret's own values (which `check no-value` cannot see while it is absent): not in the log either."""
import base64, json, sys
data, text = json.load(open(sys.argv[1]))["data"], open(sys.argv[2]).read()
for key, encoded in data.items():
    if encoded in text or base64.b64decode(encoded).decode() in text:
        sys.exit(f"FAIL: the log holds the deleted Secret's {key}")
print(f"no value of the deleted Secret in the log ({len(data)} keys checked)")
PY
$k -n "$ns" create -f "$work/inkcast-secrets.json"   # the restore the runbook describes: the Secret back whole
check agree
check same "$work/fifth" password identity sync
after="$(openfga_pod)"
[ "$before" = "$after" ] || { echo "OpenFGA's pod changed ($before -> $after) while the Secret was lost" >&2; exit 1; }
serving   # 200 with the key inkcast-secrets held before: OpenFGA kept it, nothing was made anew
echo "the run failed naming the lost values and made nothing; OpenFGA's pod untouched ($after); the Secret restored"
$k -n "$ns" delete job env-secrets-e2e-lost

$k delete namespace "$ns" inkcast-db --wait=false   # frees the node for the next step of the kind job
echo "PASS: every generated Secret made by the cluster, in agreement, copied back on a rebuild, refused on a disagreement"
