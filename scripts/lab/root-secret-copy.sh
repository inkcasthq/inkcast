#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The root Secret's copy on the lab (OpenShift Local), installed in order and proven (issue #436, ADR-0020):
#
#   scripts/lab/root-secret-copy.sh        # from a checkout, logged in to the lab as kubeadmin (`eval $(crc oc-env)`)
#
#   0. each environment's inkcast-secrets carries the label the Schedules select (inkcast.io/root-secret=true): the
#      release that carries #436 has `inkcast env-secrets` set it; until the lab runs that release, this labels it;
#   1. deploy/kustomize/shared/oadp-operator: the namespace, the OperatorGroup and the pinned Subscription. The
#      InstallPlan whose CSVs are exactly subscription.yaml's startingCSV is approved (Manual approval: a catalog's
#      move never installs another version; a plan carrying anything else stops this script, naming it), and that
#      CSV must reach Succeeded;
#   2. deploy/kustomize/shared/seaweedfs-admission, then shared/seaweedfs-access: the setup account's admission
#      policy before its one right in openshift-adp, so `create` is never in force without the policy;
#   3. deploy/kustomize/shared/seaweedfs: the namespace, the account and the setup Job (a finished Job is removed
#      first: a Job's template is immutable), which must Complete; its log names Secrets, never a value. Then the
#      policy is proven with server-side dry runs as that account: a service-account-token Secret under the
#      credential's name is denied, an Opaque one under another name is denied, the Opaque credential is admitted;
#   4. the store from SeaweedFS's own chart: the tgz scripts/versions.env pins is downloaded, checked against its
#      SHA-256, and `helm upgrade --install`ed with deploy/kustomize/shared/seaweedfs/values.yaml; the chart's
#      post-install hook makes the bucket; the all-in-one Deployment must roll out;
#   5. deploy/kustomize/shared/oadp: the DPA and the two Schedules. The DPA must be Reconciled, Velero's Deployment
#      available, the storage location root-secrets Available;
#   6. one backup from the staging Schedule, built as `velero backup create --from-schedule` builds it (the template
#      copied whole, the label velero.io/schedule-name set), which must reach Completed with one item backed up, and
#      its objects listed in the bucket by the store itself.
#
# Every step is an apply and a wait: a second run changes nothing and proves the same. It never deletes a Secret,
# a namespace or a volume, and prints no value. The drill that deletes inkcast-secrets and restores it is
# scripts/ops/root-secret-drill.sh, run by a person after the owner's yes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# The checkout's virtual environment (CONTRIBUTING.md), never the host's python3: macOS ships 3.9. INKCAST_PYTHON
# names another interpreter (CI and the tests pass their own).
py="${INKCAST_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$py" ] || { echo "no Python at $py: create the checkout's venv first (python3.14 -m venv .venv && .venv/bin/pip install -e ., CONTRIBUTING.md), or set INKCAST_PYTHON" >&2; exit 2; }
# An executable is not proof of Python: run it, before any cluster call, against pyproject.toml's requires-python.
[ "$("$py" -c 'import sys; print(sys.version_info >= (3, 12))' 2>/dev/null)" = True ] || { echo "$py is not Python 3.12 or later (pyproject.toml's requires-python)" >&2; exit 2; }
k="${KUBECTL:-oc}"
adp=openshift-adp
store_ns=inkcast-seaweedfs
release=seaweedfs
bucket=inkcast-root-secrets
setup_account="system:serviceaccount:$store_ns:seaweedfs-setup"
csv="$(sed -n 's/^ *startingCSV: *//p' "$ROOT/deploy/kustomize/shared/oadp-operator/subscription.yaml")"
[ -n "$csv" ] || { echo "subscription.yaml names no startingCSV" >&2; exit 1; }
# shellcheck disable=SC1091  # the chart's pins, three KEY=value lines, beside the kind and operator pins
source "$ROOT/scripts/versions.env"
: "${SEAWEEDFS_CHART_REPO:?}" "${SEAWEEDFS_CHART_VERSION:?}" "${SEAWEEDFS_CHART_SHA256:?}"
command -v helm >/dev/null || { echo "helm is not on PATH (the lab has /usr/local/bin/helm)" >&2; exit 1; }
work="$(mktemp -d "${TMPDIR:-/tmp}/root-secret-copy.XXXXXX")"
trap 'rm -rf "$work"' EXIT

step() { printf '\n== %s\n' "$*"; }
# Run a check until it passes, for at most the seconds given; name what was waited for on a timeout.
wait_for() {
  local deadline what; deadline=$(( $(date +%s) + $1 )); what="$2"; shift 2
  until "$@"; do
    if [ "$(date +%s)" -ge "$deadline" ]; then echo "timed out waiting for $what" >&2; return 1; fi
    sleep 5
  done
}
# A jsonpath field of an object; empty when the object is absent.
field() { $k -n "$1" get "$2" -o "jsonpath=$3" 2>/dev/null || true; }
is() { [ "$(field "$1" "$2" "$3")" = "$4" ]; }
sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1
}

step "0. the label the Schedules select, on each environment's root Secret"
for ns in inkcast-staging inkcast-prod; do
  if [ -n "$(field "$ns" secret/inkcast-secrets '{.metadata.name}')" ]; then
    $k -n "$ns" label secret inkcast-secrets inkcast.io/root-secret=true --overwrite
  else
    echo "$ns/inkcast-secrets does not exist: nothing to label (the environment is not installed)"
  fi
done

step "1. the OADP operator, pinned to $csv"
$k apply -k "$ROOT/deploy/kustomize/shared/oadp-operator"
# The InstallPlan whose CSVs are exactly the pinned one. A plan that carries the pinned CSV beside another (a
# catalog's bundle of upgrades) is named and stops the script: approving it would install what the pin never named.
plan_for_csv() {
  $k -n "$adp" get installplan -o json 2>/dev/null | "$py" -c '
import json, sys
wanted = sys.argv[1]
for plan in json.load(sys.stdin).get("items", []):
    names = plan["spec"].get("clusterServiceVersionNames", [])
    if names == [wanted]:
        print(plan["metadata"]["name"]); break
    if wanted in names:
        name = plan["metadata"]["name"]
        sys.stderr.write(f"InstallPlan {name} carries {wanted} beside {[n for n in names if n != wanted]}: not approved. Approve by hand what you mean, or wait for a plan of the pinned version alone.\n"); sys.exit(3)' "$csv"
}
# A refusal (status 3) stops the script here: in a condition (`until`) `set -e` ignores it, and the wait would run
# out its deadline repeating it. Any other failure (the API, an empty answer) is waited through, as before.
has_plan() { local found status=0; found="$(plan_for_csv)" || status=$?; [ "$status" -ne 3 ] || exit 1; [ -n "$found" ]; }
wait_for 300 "OLM's InstallPlan for $csv alone" has_plan
plan="$(plan_for_csv)"
if [ "$(field "$adp" "installplan/$plan" '{.spec.approved}')" != "true" ]; then
  $k -n "$adp" patch installplan "$plan" --type merge -p '{"spec":{"approved":true}}'
fi
echo "InstallPlan $plan approved ($csv alone)"
csv_succeeded() { is "$adp" "csv/$csv" '{.status.phase}' Succeeded; }
wait_for 600 "CSV $csv to succeed" csv_succeeded
echo "CSV $csv: $(field "$adp" "csv/$csv" '{.status.phase}') ($(field "$adp" "csv/$csv" '{.spec.version}'))"

step "2. the setup account's admission policy, then its right in $adp"
$k apply -k "$ROOT/deploy/kustomize/shared/seaweedfs-admission"
$k apply -k "$ROOT/deploy/kustomize/shared/seaweedfs-access"

step "3. the store's identity: the setup Job"
if [ -n "$(field "$store_ns" job/seaweedfs-setup '{.metadata.name}')" ]; then
  $k -n "$store_ns" delete job seaweedfs-setup --wait=true   # immutable template: a new Job per apply
fi
$k apply -k "$ROOT/deploy/kustomize/shared/seaweedfs"
job_done() {
  if is "$store_ns" job/seaweedfs-setup '{.status.conditions[?(@.type=="Failed")].status}' True; then
    echo "the setup Job failed:" >&2; $k -n "$store_ns" logs job/seaweedfs-setup --all-containers >&2 || true; exit 1
  fi
  is "$store_ns" job/seaweedfs-setup '{.status.conditions[?(@.type=="Complete")].status}' True
}
wait_for 900 "the setup Job to complete" job_done
$k -n "$store_ns" logs job/seaweedfs-setup
for secret in "$store_ns/seaweedfs-s3-config" "$adp/cloud-credentials-root-secrets"; do
  if [ -n "$(field "${secret%/*}" "secret/${secret#*/}" '{.metadata.name}')" ]; then
    echo "Secret $secret: present"
  else
    echo "Secret $secret is absent after the Job" >&2; exit 1
  fi
done
# The admission policy, proven as that account with server-side dry runs (nothing is created). The API validates a
# Secret before an admission policy sees it, and a service-account-token Secret must carry the account's annotation
# (measured: without it the API answers "Required value" and the policy never runs), so the token probe carries it,
# as an attacker's would. Denied: a service-account-token Secret for Velero's account under the credential's name;
# an Opaque Secret under another name; the credential annotated for a service account. Admitted: the Opaque
# credential under its own name (the answer is then "already exists" or "created (server dry run)", never the
# policy's refusal).
probe() {  # namespace name type [service account to annotate for]: the server's answer to a dry-run create as the setup account
  local annotations=""
  if [ -n "${4:-}" ]; then annotations=",\"annotations\":{\"kubernetes.io/service-account.name\":\"$4\"}"; fi
  printf '{"apiVersion":"v1","kind":"Secret","type":"%s","metadata":{"name":"%s","namespace":"%s"%s},"data":{"probe":"eA=="}}' \
    "$3" "$2" "$1" "$annotations" | $k create -f - --dry-run=server --as="$setup_account" -o name 2>&1 || true
}
said="$(probe "$adp" cloud-credentials-root-secrets kubernetes.io/service-account-token velero)"
case "$said" in *"creates Opaque Secrets only"*) echo "admission: a service-account-token Secret for velero, as $setup_account, is denied" ;;
  *) echo "admission: a service-account-token Secret as $setup_account was NOT denied: $said" >&2; exit 1 ;; esac
said="$(probe "$store_ns" some-other-secret Opaque)"
case "$said" in *"creates only"*) echo "admission: an Opaque Secret under another name, as $setup_account, is denied" ;;
  *) echo "admission: another name as $setup_account was NOT denied: $said" >&2; exit 1 ;; esac
said="$(probe "$adp" cloud-credentials-root-secrets Opaque velero)"
case "$said" in *"no Secret annotated for a service account"*) echo "admission: the credential annotated for a service account, as $setup_account, is denied" ;;
  *) echo "admission: the annotated credential as $setup_account was NOT denied: $said" >&2; exit 1 ;; esac
said="$(probe "$adp" cloud-credentials-root-secrets Opaque)"
case "$said" in *"seaweedfs-setup creates"*) echo "admission: the Opaque credential under its own name was denied: $said" >&2; exit 1 ;;
  *) echo "admission: the Opaque credential under its own name is admitted ($(printf '%s' "$said" | head -n 1 | cut -c1-80))" ;; esac

step "4. the store from its chart, $SEAWEEDFS_CHART_VERSION, checked against scripts/versions.env"
tgz="$work/seaweedfs-$SEAWEEDFS_CHART_VERSION.tgz"
curl -fsSL --retry 3 -o "$tgz" "$SEAWEEDFS_CHART_REPO/seaweedfs-$SEAWEEDFS_CHART_VERSION.tgz"
got="$(sha256 "$tgz")"
if [ "$got" != "$SEAWEEDFS_CHART_SHA256" ]; then
  echo "seaweedfs-$SEAWEEDFS_CHART_VERSION.tgz: SHA-256 $got is not the pinned $SEAWEEDFS_CHART_SHA256; not installed" >&2; exit 1
fi
echo "seaweedfs-$SEAWEEDFS_CHART_VERSION.tgz: SHA-256 as pinned"
helm upgrade --install "$release" "$tgz" --version "$SEAWEEDFS_CHART_VERSION" -n "$store_ns" \
  -f "$ROOT/deploy/kustomize/shared/seaweedfs/values.yaml" --wait --timeout 10m
helm -n "$store_ns" list --filter "^$release\$"
$k -n "$store_ns" rollout status "deploy/$release-all-in-one" --timeout=300s
$k -n "$store_ns" get networkpolicy

step "5. the DPA, Velero, the storage location and the Schedules"
$k apply -k "$ROOT/deploy/kustomize/shared/oadp"
dpa_reconciled() { is "$adp" dpa/inkcast '{.status.conditions[?(@.type=="Reconciled")].status}' True; }
wait_for 300 "the DPA to be Reconciled" dpa_reconciled || { $k -n "$adp" get dpa inkcast -o jsonpath='{.status.conditions}' >&2; echo >&2; exit 1; }
$k -n "$adp" rollout status deploy/velero --timeout=300s
bsl_available() { is "$adp" backupstoragelocations.velero.io/root-secrets '{.status.phase}' Available; }
wait_for 300 "the storage location root-secrets to be Available" bsl_available \
  || { $k -n "$adp" get backupstoragelocations.velero.io root-secrets -o jsonpath='{.status}' >&2; echo >&2; exit 1; }
$k -n "$adp" get backupstoragelocations.velero.io root-secrets
$k -n "$adp" get schedules.velero.io
$k -n "$adp" get pod -l app.kubernetes.io/name=velero -o custom-columns='NAME:.metadata.name,LABELS:.metadata.labels' --no-headers | cut -c1-200

step "6. one backup from the staging Schedule, and its objects in the bucket"
backup="inkcast-staging-root-secret-$(date -u +%Y%m%d%H%M%S)"
# As `velero backup create --from-schedule`: the Schedule's template is the Backup's spec, its labels are copied,
# velero.io/schedule-name is set (pkg/builder/backup_builder.go FromSchedule at v1.17.0).
$k -n "$adp" get schedules.velero.io inkcast-staging-root-secret -o json | "$py" -c '
import json, sys
schedule = json.load(sys.stdin)
labels = dict(schedule["metadata"].get("labels", {}))
labels["velero.io/schedule-name"] = schedule["metadata"]["name"]
print(json.dumps({"apiVersion": "velero.io/v1", "kind": "Backup",
                  "metadata": {"name": sys.argv[1], "namespace": schedule["metadata"]["namespace"], "labels": labels},
                  "spec": schedule["spec"]["template"]}))' "$backup" | $k create -f -
backup_done() {
  case "$(field "$adp" "backups.velero.io/$backup" '{.status.phase}')" in
    Completed) return 0 ;;
    ""|New|InProgress|WaitingForPluginOperations|Finalizing) return 1 ;;
    *) echo "backup $backup: $(field "$adp" "backups.velero.io/$backup" '{.status.phase}'): $(field "$adp" "backups.velero.io/$backup" '{.status.failureReason}')" >&2
       $k -n "$adp" logs deploy/velero | grep "$backup" | tail -20 >&2 || true; exit 1 ;;
  esac
}
wait_for 300 "backup $backup to complete" backup_done
items="$(field "$adp" "backups.velero.io/$backup" '{.status.progress.itemsBackedUp}')"
echo "backup $backup: Completed, $items item(s), errors $(field "$adp" "backups.velero.io/$backup" '{.status.errors}'), warnings $(field "$adp" "backups.velero.io/$backup" '{.status.warnings}'), expires $(field "$adp" "backups.velero.io/$backup" '{.status.expiration}')"
[ "${items:-0}" -ge 1 ] || { echo "the backup holds no item: is $(printf 'inkcast-staging/inkcast-secrets') labelled?" >&2; exit 1; }
# The store lists its own bucket (weed shell on the pod, through the local sockets: no credential needed and none shown).
listing="$($k -n "$store_ns" exec "deploy/$release-all-in-one" -- sh -c "printf 'fs.ls /buckets/$bucket/velero/backups/$backup/\n' | weed shell -master=localhost:9333 -filer=localhost:8888" 2>&1)"
printf '%s\n' "$listing" | sed 's/^/  /'
case "$listing" in
  *velero-backup.json*"$backup.tar.gz"*|*"$backup.tar.gz"*velero-backup.json*) echo "objects of $backup are in bucket $bucket" ;;
  *) echo "the bucket does not list the backup's objects" >&2; exit 1 ;;
esac
echo
echo "The copy is in place. The drill (after the owner's yes): scripts/ops/root-secret-drill.sh inkcast-staging"
