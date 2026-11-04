#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The restore drill for the root Secret's copy (issue #436, acceptance 3; ADR-0020): a deleted inkcast-secrets comes
# back from the cluster backup, unchanged, and the environment runs on it.
#
#   scripts/ops/root-secret-drill.sh <inkcast-staging|inkcast-prod>     # the lab, after the owner's yes
#
# 1. a safety copy of the Secret on this host only (mode 600, removed at the end), so a failed restore is not a loss:
#    a drill that stops after step 3 puts an absent Secret back from it, and keeps it, naming it, when the Secret is
#    present with other values; its values are never printed, only each one's SHA-256 prefix; and a marker sealed
#    by the server's pod with the key it mounts, to be unsealed after the restore;
# 2. a backup from the environment's Schedule, built as `velero backup create --from-schedule` builds it, waited to
#    Completed with one item;
# 3. THE LOSS: `oc -n <namespace> delete secret inkcast-secrets`. The one destructive step, and the only Secret this
#    script ever deletes;
# 4. the restore: a Velero Restore of that backup limited to `secrets` carrying inkcast.io/root-secret=true in that
#    namespace (existingResourcePolicy none: Velero never overwrites what exists), waited to Completed with no error;
# 5. the Secret is back with every key, each value's SHA-256 equal to the copy's, the label on it;
# 6. the CronJob's run now: a Job from inkcast-env-secrets must Complete and its log must end with "every generated
#    Secret is present and in agreement";
# 7. sealed credentials still read: inside the server's pod, with the repository's own Sealer and the key file the
#    pod mounts, the marker from step 1 is unsealed and so is every row of the credentials table. No endpoint and
#    no command unseals on demand, so the server's own Python is asked, as scripts/ops/backup-drill.sh asks it for SQL.
#
# No value is printed by any step: hashes, names, counts and phases only.
set -euo pipefail
ns="${1:?usage: root-secret-drill.sh <inkcast-staging|inkcast-prod>}"
k="${KUBECTL:-oc}"
adp="${VELERO_NAMESPACE:-openshift-adp}"
secret=inkcast-secrets
label_key=inkcast.io/root-secret
# The label's JSONPath: a dot in a key is escaped. kubectl's and oc's `['inkcast.io/root-secret']` answer nothing
# (measured with kubectl 1.37 and oc 4.22), so a labelled Secret would read as unlabelled.
label_path="{.metadata.labels.${label_key//./\\.}}"
case "$ns" in
  inkcast-staging) schedule=inkcast-staging-root-secret ;;
  inkcast-prod) schedule=inkcast-prod-root-secret ;;
  *) echo "usage: root-secret-drill.sh <inkcast-staging|inkcast-prod>" >&2; exit 2 ;;
esac
stamp="$(date -u +%Y%m%d%H%M%S)"
umask 077
work="$(mktemp -d "${TMPDIR:-/tmp}/root-secret-drill.XXXXXX")"
deleted="" restored=""
# At the end, however it ends. Between the loss (step 3) and the proof that the Secret is back the same (step 5) the
# safety copy is the only one the drill holds: an absent Secret is put back from it, and a Secret that is present
# with other values (made meanwhile: OpenFGA's init container makes one when its pod starts without it) leaves the
# copy kept, named, for a person. Otherwise the copy is removed.
finish() {
  if [ -n "$deleted" ] && [ -z "$restored" ]; then
    if [ -z "$(field "$ns" "secret/$secret" '{.metadata.name}')" ] && $k -n "$ns" create -f "$work/put-back.json" > /dev/null; then
      echo "the drill stopped after deleting $ns/$secret: it was put back from the safety copy" >&2
    else
      echo "the drill stopped after deleting $ns/$secret, and it was not put back: the safety copy is kept in $work" \
        "(mode 600). The values the sealed credentials need are its: $k -n $ns replace -f $work/put-back.json" \
        "(or create, if the Secret is absent), then rm -rf $work." >&2
      return
    fi
  fi
  rm -rf "$work"
}
trap finish EXIT

step() { printf '\n%s\n' "$*"; }
wait_for() {
  local deadline what; deadline=$(( $(date +%s) + $1 )); what="$2"; shift 2
  until "$@"; do
    if [ "$(date +%s)" -ge "$deadline" ]; then echo "timed out waiting for $what" >&2; return 1; fi
    sleep 5
  done
}
field() { $k -n "$1" get "$2" -o "jsonpath=$3" 2>/dev/null || true; }
# "key hash12" per data key of a Secret read as JSON on stdin, sorted; never a value.
hashes() {
  python3 -c '
import base64, hashlib, json, sys
for key, value in sorted(json.load(sys.stdin).get("data", {}).items()):
    print(key, hashlib.sha256(base64.b64decode(value)).hexdigest()[:12])'
}
# Inside the server's pod: seal a marker (argument "seal <text>"), or unseal one and every stored credential
# ("unseal <sealed>"), with the key file the pod mounts, read as the server reads it (stripped).
in_pod() {
  $k -n "$ns" exec deploy/inkcast -- python -c '
import os, pathlib, sqlite3, sys
from inkcast.libs.secrets import Sealer
sealer = Sealer(pathlib.Path(os.environ["INKCAST_SECRET_KEY_FILE"]).read_text(encoding="utf-8").strip())
if sys.argv[1] == "seal":
    print(sealer.seal({"drill": sys.argv[2]}))
else:
    print("marker:", sealer.unseal(sys.argv[2])["drill"])
    rows = sqlite3.connect("file:" + os.environ.get("INKCAST_DATABASE", "/data/inkcast.db") + "?mode=ro", uri=True) \
        .execute("SELECT sealed FROM credentials").fetchall()
    for (sealed,) in rows:
        sealer.unseal(sealed)
    print(f"{len(rows)} of {len(rows)} sealed credentials read" if rows else "no sealed credential stored: nothing to read")' "$@"
}

step "1. the safety copy, on this host only ($work, mode 600, removed at the end)"
$k -n "$ns" get secret "$secret" -o json > "$work/copy.json"
hashes < "$work/copy.json" > "$work/before"
# What a create takes back (no resourceVersion, uid or owner): the type, the name, the labels and the data.
python3 -c '
import json, sys
secret = json.load(sys.stdin)
meta = secret["metadata"]
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "type": secret.get("type", "Opaque"), "data": secret.get("data", {}),
                  "metadata": {"name": meta["name"], "namespace": meta["namespace"], "labels": meta.get("labels", {})}}))' \
  < "$work/copy.json" > "$work/put-back.json"
echo "copied $secret: $(wc -l < "$work/before" | tr -d ' ') keys ($(cut -d' ' -f1 "$work/before" | tr '\n' ' '))"
[ "$(field "$ns" "secret/$secret" "$label_path")" = true ] || { echo "$ns/$secret lacks $label_key=true: the backup would hold nothing. Label it first (the release with #436 does; scripts/lab/root-secret-copy.sh step 0)." >&2; exit 1; }
in_pod seal "$stamp" > "$work/marker"
echo "a marker sealed by the server with the key it mounts"

step "2. a backup from Schedule $schedule"
backup="$schedule-$stamp"
$k -n "$adp" get schedules.velero.io "$schedule" -o json | python3 -c '
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
    *) echo "backup $backup: $(field "$adp" "backups.velero.io/$backup" '{.status.phase}'): $(field "$adp" "backups.velero.io/$backup" '{.status.failureReason}')" >&2; exit 1 ;;
  esac
}
wait_for 300 "backup $backup to complete" backup_done
items="$(field "$adp" "backups.velero.io/$backup" '{.status.progress.itemsBackedUp}')"
echo "backup $backup: Completed, $items item(s)"
[ "${items:-0}" -ge 1 ] || { echo "the backup holds no item" >&2; exit 1; }

step "3. THE LOSS: deleting $ns/$secret"
$k -n "$ns" delete secret "$secret"
deleted=yes
[ -z "$(field "$ns" "secret/$secret" '{.metadata.name}')" ] || { echo "$secret still exists" >&2; exit 1; }

step "4. the restore, limited to secrets carrying $label_key=true in $ns"
restore="$backup-restore"
python3 -c '
import json, sys
print(json.dumps({"apiVersion": "velero.io/v1", "kind": "Restore",
                  "metadata": {"name": sys.argv[1], "namespace": sys.argv[2]},
                  "spec": {"backupName": sys.argv[3], "includedNamespaces": [sys.argv[4]], "includedResources": ["secrets"],
                           "labelSelector": {"matchLabels": {sys.argv[5]: "true"}},
                           "existingResourcePolicy": "none", "includeClusterResources": False, "restorePVs": False}}))' \
  "$restore" "$adp" "$backup" "$ns" "$label_key" | $k create -f -
restore_done() {
  case "$(field "$adp" "restores.velero.io/$restore" '{.status.phase}')" in
    Completed) return 0 ;;
    ""|New|InProgress|WaitingForPluginOperations|Finalizing) return 1 ;;
    *) echo "restore $restore: $(field "$adp" "restores.velero.io/$restore" '{.status.phase}'): $(field "$adp" "restores.velero.io/$restore" '{.status.failureReason}')" >&2; exit 1 ;;
  esac
}
wait_for 300 "restore $restore to complete" restore_done
errors="$(field "$adp" "restores.velero.io/$restore" '{.status.errors}')"
echo "restore $restore: Completed, errors ${errors:-0}, warnings $(field "$adp" "restores.velero.io/$restore" '{.status.warnings}')"
[ "${errors:-0}" = 0 ] || { $k -n "$adp" describe restores.velero.io "$restore" >&2; exit 1; }

step "5. the Secret is back, every value the same (by SHA-256), the label on it"
$k -n "$ns" get secret "$secret" -o json | hashes > "$work/after"
if diff "$work/before" "$work/after" > "$work/diff"; then
  sed 's/^/  same: /' "$work/after"
else
  echo "the restored Secret differs from the copy (key hash):" >&2; cat "$work/diff" >&2; exit 1
fi
if [ "$(field "$ns" "secret/$secret" "$label_path")" = true ]; then
  echo "  label $label_key=true: present"
  restored=yes
else
  echo "the restored Secret lacks $label_key" >&2; exit 1
fi

step "6. the CronJob's run now"
job="root-secret-drill-$stamp"
$k -n "$ns" create job --from=cronjob/inkcast-env-secrets "$job"
job_done() {
  if [ "$(field "$ns" "job/$job" '{.status.conditions[?(@.type=="Failed")].status}')" = True ]; then
    echo "the run failed:" >&2; $k -n "$ns" logs "job/$job" >&2 || true; exit 1
  fi
  [ "$(field "$ns" "job/$job" '{.status.conditions[?(@.type=="Complete")].status}')" = True ]
}
wait_for 300 "the env-secrets run to complete" job_done
log="$($k -n "$ns" logs "job/$job")"
printf '%s\n' "$log" | sed 's/^/  /'
case "$log" in
  *"every generated Secret is present and in agreement."*) echo "the run: Complete, in agreement" ;;
  *) echo "the run completed without saying every Secret is in agreement" >&2; exit 1 ;;
esac

step "7. sealed credentials still read, with the key the pod mounts"
in_pod unseal "$(cat "$work/marker")" | sed 's/^/  /'

echo
echo "Drill passed: backed up, lost, restored the same, the CronJob agrees, sealed credentials read."
