#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# Restore the Inkcast SQLite database from the backups volume (the `backup` component), in a Kubernetes or
# OpenShift namespace. Runbook: docs/runbooks/backup-restore.md.
#
#   scripts/ops/restore.sh <namespace> [backup-file-name]     # default: the newest backup
#   scripts/ops/restore.sh <namespace> --list                 # list the backups (the server keeps running)
#
# Steps, each checked: scale the server to 0 (a restore refuses while the server runs), run `inkcast restore` as a
# one-off Job with the same image and both volumes, scale back to the previous replica count, wait for the rollout.
# The database being replaced is kept on the data volume as inkcast.db.before-restore-<time>; its erasure records
# go into the restored copy, and the server erases those people again as it starts, before it serves (the runbook
# says what that covers and what it can't). KUBECTL=oc works too.
#
# The server and the restore Job never run at the same time. The Job gets RESTORE_TIMEOUT seconds (default 300)
# as its activeDeadlineSeconds, and the script waits that long for it; a Job that has not completed by then, or
# that failed, or that is still there when the script is interrupted, is deleted (with its pod) before the server
# is scaled back up. If its pod cannot be confirmed gone, the server stays at 0 replicas and the script says so.
set -euo pipefail
ns="${1:?usage: restore.sh <namespace> [backup-file-name]}"
which_backup="${2:-}"
k="${KUBECTL:-kubectl}"
timeout="${RESTORE_TIMEOUT:-300}"
case "$timeout" in
  ''|*[!0-9]*|0*) echo "RESTORE_TIMEOUT must be a whole number of seconds above 0 (got '$timeout')." >&2; exit 2 ;;
esac
job="inkcast-restore-$(date -u +%Y%m%d%H%M%S)"

image="$($k -n "$ns" get cronjob inkcast-backup -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')"
policy="$($k -n "$ns" get cronjob inkcast-backup -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].imagePullPolicy}')"
security="$($k -n "$ns" get cronjob inkcast-backup -o jsonpath='{.spec.jobTemplate.spec.template.spec.securityContext}')"
[ -n "$security" ] || security='{"runAsNonRoot": true, "seccompProfile": {"type": "RuntimeDefault"}}'
[ -n "$policy" ] || policy=IfNotPresent
replicas="$($k -n "$ns" get deploy inkcast -o jsonpath='{.spec.replicas}')"
# --force: this script has just scaled the server to 0 and waited for its pod to go, so the scheduler's last
# heartbeat (seconds old) is not a running server. By hand, `inkcast restore` keeps that guard.
listing=false
if [ "$which_backup" = "--list" ]; then listing=true; job="inkcast-backups-$(date -u +%Y%m%d%H%M%S)"
  args='["backups", "--to", "/backups"]'
elif [ -n "$which_backup" ]; then args="[\"restore\", \"/backups/$which_backup\", \"--force\"]"
else args='["restore", "--latest", "/backups", "--force"]'; fi

# What the script has done so far, for the exit trap: whether it scaled the server to 0, whether the Job exists,
# and whether it reached its end (then the trap has nothing to do).
scaled_down=false
applied=false
finished=false

job_condition() {
  # "True" when the Job's condition $1 (Complete, Failed) is set; empty otherwise, or when it cannot be read.
  $k -n "$ns" get job "$job" -o jsonpath="{.status.conditions[?(@.type==\"$1\")].status}" 2>/dev/null || true
}

stop_job() {
  # Delete the Job and its pod (foreground cascade: the Job goes only after its pod has), then confirm nothing of
  # it is left. Returns 1 if a pod is still there: the server must not come back then.
  echo "Deleting job $job and its pod."
  $k -n "$ns" delete job "$job" --cascade=foreground --wait=true --timeout=120s --ignore-not-found || true
  $k -n "$ns" wait --for=delete pod -l "job-name=$job" --timeout=60s 2>/dev/null || true
  applied=false
  local left
  left="$($k -n "$ns" get pod -l "job-name=$job" -o name)" || left="(could not list the pods of job $job)"
  if [ -n "$left" ]; then
    echo "A pod of job $job is still there ($left): the server stays at 0 replicas, so the restore cannot swap" \
         "the database under it. Remove the pod, then scale the server back: $k -n $ns scale deploy/inkcast" \
         "--replicas=$replicas" >&2
    return 1
  fi
}

scale_back() {
  echo "Scaling the server back to $replicas."
  $k -n "$ns" scale deploy/inkcast --replicas="$replicas"
  scaled_down=false
}

on_exit() {
  local rc=$?
  trap - EXIT
  if $finished; then exit "$rc"; fi
  [ "$rc" -ne 0 ] || rc=1
  echo "restore.sh stopped before it finished." >&2
  if $applied && ! $listing; then
    if [ "$(job_condition Complete)" = "True" ]; then
      echo "The restore job had already completed: the database was replaced." >&2
    else
      if ! stop_job; then exit "$rc"; fi
      echo "The restore job was stopped: nothing was replaced." >&2
    fi
  fi
  if $scaled_down; then scale_back || true; fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

if ! $listing; then
  echo "Scaling the server to 0 (it was $replicas)."
  scaled_down=true            # before the call: interrupted while it runs, the server may be at 0 already
  $k -n "$ns" scale deploy/inkcast --replicas=0
  $k -n "$ns" wait --for=delete pod -l app.kubernetes.io/component=server --timeout=180s || true
  # `|| true` covers "no pods to wait for"; what --force relies on is that none is left (a list that fails is not).
  servers="$($k -n "$ns" get pod -l app.kubernetes.io/component=server -o name)" ||
    servers="(could not list the server's pods)"
  if [ -n "$servers" ]; then
    echo "The server's pod is still there ($servers): not restoring under a live server. Scaling back to $replicas." >&2
    scale_back
    finished=true
    exit 1
  fi
fi

echo "Restoring with $image."
applied=true
$k -n "$ns" apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $job
  labels: {app.kubernetes.io/name: inkcast, app.kubernetes.io/component: backup}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $timeout
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels: {app.kubernetes.io/name: inkcast, app.kubernetes.io/component: backup}
    spec:
      restartPolicy: Never
      serviceAccountName: inkcast
      automountServiceAccountToken: false
      enableServiceLinks: false
      securityContext: $security
      containers:
        - name: restore
          image: $image
          imagePullPolicy: $policy
          args: $args
          env:
            - {name: INKCAST_DATABASE, value: /data/inkcast.db}
            - {name: INKCAST_LOG_FORMAT, value: json}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: backups, mountPath: /backups, readOnly: true}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: inkcast-data}
        - name: backups
          persistentVolumeClaim: {claimName: inkcast-backups}
        - name: tmp
          emptyDir: {sizeLimit: 64Mi}
EOF
# `kubectl wait` exits 0 only once the Job's Complete condition is true: that is the one path that reports a restore.
completed=false
if $k -n "$ns" wait --for=condition=complete "job/$job" --timeout="${timeout}s"; then completed=true; applied=false; fi
$k -n "$ns" logs "job/$job" || true
if $listing; then
  finished=true
  $completed && exit 0
  exit 1
fi

if ! $completed; then
  if [ "$(job_condition Failed)" = "True" ]; then
    echo "The restore job failed (its log is above)." >&2
  else
    echo "The restore job did not complete within $timeout s: stopping it before the server comes back." >&2
  fi
  # Either way the Job and its pod go before the server does anything, so no late restore can swap the database
  # under it. If the pod will not go, the server stays down (stop_job says so) and this exits without scaling up.
  if ! stop_job; then finished=true; exit 1; fi
  scale_back
  finished=true
  echo "Nothing was replaced: the server runs on the database it had." >&2
  exit 1
fi

scale_back
finished=true
if ! $k -n "$ns" rollout status deploy/inkcast --timeout=180s; then
  echo "The database was replaced, but the server's rollout did not finish within 180 s: check its pods" \
       "($k -n $ns get pod -l app.kubernetes.io/component=server) and its log." >&2
  exit 1
fi
echo "Restored. The previous database is on the data volume as inkcast.db.before-restore-<time>."
