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
#      InstallPlan that carries subscription.yaml's startingCSV is approved (Manual approval: a catalog's move never
#      installs another version), and that CSV must reach Succeeded;
#   2. deploy/kustomize/shared/seaweedfs-access: the setup Job's one right in openshift-adp;
#   3. deploy/kustomize/shared/seaweedfs: the store, its volume, its policy and the setup Job (a finished Job is
#      removed first: a Job's template is immutable), which must Complete; its log names Secrets, never a value;
#   4. deploy/kustomize/shared/oadp: the DPA and the two Schedules. The DPA must be Reconciled, Velero's Deployment
#      available, the storage location root-secrets Available;
#   5. one backup from the staging Schedule, built as `velero backup create --from-schedule` builds it (the template
#      copied whole, the label velero.io/schedule-name set), which must reach Completed with one item backed up, and
#      its objects listed in the bucket by the store itself.
#
# Every step is an apply and a wait: a second run changes nothing and proves the same. It never deletes a Secret,
# a namespace or a volume, and prints no value. The drill that deletes inkcast-secrets and restores it is
# scripts/ops/root-secret-drill.sh, run by a person after the owner's yes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
k="${KUBECTL:-oc}"
adp=openshift-adp
store_ns=inkcast-seaweedfs
bucket=inkcast-root-secrets
csv="$(sed -n 's/^ *startingCSV: *//p' "$ROOT/deploy/kustomize/shared/oadp-operator/subscription.yaml")"
[ -n "$csv" ] || { echo "subscription.yaml names no startingCSV" >&2; exit 1; }

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
plan_for_csv() {   # the InstallPlan carrying the pinned CSV, when OLM has made it
  $k -n "$adp" get installplan -o json 2>/dev/null | python3 -c '
import json, sys
wanted = sys.argv[1]
for plan in json.load(sys.stdin).get("items", []):
    if wanted in plan["spec"].get("clusterServiceVersionNames", []):
        print(plan["metadata"]["name"]); break' "$csv"
}
has_plan() { [ -n "$(plan_for_csv)" ]; }
wait_for 300 "OLM's InstallPlan for $csv" has_plan
plan="$(plan_for_csv)"
if [ "$(field "$adp" "installplan/$plan" '{.spec.approved}')" != "true" ]; then
  $k -n "$adp" patch installplan "$plan" --type merge -p '{"spec":{"approved":true}}'
fi
echo "InstallPlan $plan approved"
csv_succeeded() { is "$adp" "csv/$csv" '{.status.phase}' Succeeded; }
wait_for 600 "CSV $csv to succeed" csv_succeeded
echo "CSV $csv: $(field "$adp" "csv/$csv" '{.status.phase}') ($(field "$adp" "csv/$csv" '{.spec.version}'))"

step "2. the setup Job's right in $adp"
$k apply -k "$ROOT/deploy/kustomize/shared/seaweedfs-access"

step "3. the store, and its setup Job"
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
$k -n "$store_ns" rollout status deploy/seaweedfs --timeout=300s
for secret in "$store_ns/seaweedfs-s3-identities" "$adp/cloud-credentials-root-secrets"; do
  if [ -n "$(field "${secret%/*}" "secret/${secret#*/}" '{.metadata.name}')" ]; then
    echo "Secret $secret: present"
  else
    echo "Secret $secret is absent after the Job" >&2; exit 1
  fi
done

step "4. the DPA, Velero, the storage location and the Schedules"
$k apply -k "$ROOT/deploy/kustomize/shared/oadp"
dpa_reconciled() { is "$adp" dpa/inkcast '{.status.conditions[?(@.type=="Reconciled")].status}' True; }
wait_for 300 "the DPA to be Reconciled" dpa_reconciled || { $k -n "$adp" get dpa inkcast -o jsonpath='{.status.conditions}' >&2; echo >&2; exit 1; }
$k -n "$adp" rollout status deploy/velero --timeout=300s
bsl_available() { is "$adp" backupstoragelocation/root-secrets '{.status.phase}' Available; }
wait_for 300 "the storage location root-secrets to be Available" bsl_available \
  || { $k -n "$adp" get backupstoragelocation root-secrets -o jsonpath='{.status}' >&2; echo >&2; exit 1; }
$k -n "$adp" get backupstoragelocation root-secrets
$k -n "$adp" get schedule
$k -n "$adp" get pod -l app.kubernetes.io/name=velero -o custom-columns='NAME:.metadata.name,LABELS:.metadata.labels' --no-headers | cut -c1-200

step "5. one backup from the staging Schedule, and its objects in the bucket"
backup="inkcast-staging-root-secret-$(date -u +%Y%m%d%H%M%S)"
# As `velero backup create --from-schedule`: the Schedule's template is the Backup's spec, its labels are copied,
# velero.io/schedule-name is set (pkg/builder/backup_builder.go FromSchedule at v1.17.0).
$k -n "$adp" get schedule inkcast-staging-root-secret -o json | python3 -c '
import json, sys
schedule = json.load(sys.stdin)
labels = dict(schedule["metadata"].get("labels", {}))
labels["velero.io/schedule-name"] = schedule["metadata"]["name"]
print(json.dumps({"apiVersion": "velero.io/v1", "kind": "Backup",
                  "metadata": {"name": sys.argv[1], "namespace": schedule["metadata"]["namespace"], "labels": labels},
                  "spec": schedule["spec"]["template"]}))' "$backup" | $k create -f -
backup_done() {
  case "$(field "$adp" "backup/$backup" '{.status.phase}')" in
    Completed) return 0 ;;
    ""|New|InProgress|WaitingForPluginOperations|Finalizing) return 1 ;;
    *) echo "backup $backup: $(field "$adp" "backup/$backup" '{.status.phase}'): $(field "$adp" "backup/$backup" '{.status.failureReason}')" >&2
       $k -n "$adp" logs deploy/velero | grep "$backup" | tail -20 >&2 || true; exit 1 ;;
  esac
}
wait_for 300 "backup $backup to complete" backup_done
items="$(field "$adp" "backup/$backup" '{.status.progress.itemsBackedUp}')"
echo "backup $backup: Completed, $items item(s), errors $(field "$adp" "backup/$backup" '{.status.errors}'), warnings $(field "$adp" "backup/$backup" '{.status.warnings}'), expires $(field "$adp" "backup/$backup" '{.status.expiration}')"
[ "${items:-0}" -ge 1 ] || { echo "the backup holds no item: is $(printf 'inkcast-staging/inkcast-secrets') labelled?" >&2; exit 1; }
# The store lists its own bucket (weed shell on the pod, through the local sockets: no credential needed and none shown).
listing="$($k -n "$store_ns" exec deploy/seaweedfs -- sh -c "printf 'fs.ls /buckets/$bucket/velero/backups/$backup/\n' | weed shell -master=localhost:9333 -filer=localhost:8888" 2>&1)"
printf '%s\n' "$listing" | sed 's/^/  /'
case "$listing" in
  *velero-backup.json*"$backup.tar.gz"*|*"$backup.tar.gz"*velero-backup.json*) echo "objects of $backup are in bucket $bucket" ;;
  *) echo "the bucket does not list the backup's objects" >&2; exit 1 ;;
esac
echo
echo "The copy is in place. The drill (after the owner's yes): scripts/ops/root-secret-drill.sh inkcast-staging"
