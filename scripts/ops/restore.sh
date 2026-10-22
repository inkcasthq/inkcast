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
# The database being replaced is kept on the data volume as inkcast.db.before-restore-<time>. KUBECTL=oc works too.
set -euo pipefail
ns="${1:?usage: restore.sh <namespace> [backup-file-name]}"
which_backup="${2:-}"
k="${KUBECTL:-kubectl}"
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

if ! $listing; then
  echo "Scaling the server to 0 (it was $replicas)."
  $k -n "$ns" scale deploy/inkcast --replicas=0
  $k -n "$ns" wait --for=delete pod -l app.kubernetes.io/component=server --timeout=180s || true
  # `|| true` covers "no pods to wait for"; what --force relies on is that none is left.
  if [ -n "$($k -n "$ns" get pod -l app.kubernetes.io/component=server -o name)" ]; then
    echo "The server's pod is still there: not restoring under a live server. Scaling back to $replicas." >&2
    $k -n "$ns" scale deploy/inkcast --replicas="$replicas"
    exit 1
  fi
fi

echo "Restoring with $image."
$k -n "$ns" apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $job
  labels: {app.kubernetes.io/name: inkcast, app.kubernetes.io/component: backup}
spec:
  backoffLimit: 0
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
status=0
$k -n "$ns" wait --for=condition=complete "job/$job" --timeout=300s || status=1
$k -n "$ns" logs "job/$job" || true
if $listing; then exit "$status"; fi

echo "Scaling the server back to $replicas."
$k -n "$ns" scale deploy/inkcast --replicas="$replicas"
$k -n "$ns" rollout status deploy/inkcast --timeout=180s
if [ "$status" -ne 0 ]; then
  echo "The restore job failed: the server runs on the database it had (nothing was replaced)." >&2
  exit 1
fi
echo "Restored. The previous database is on the data volume as inkcast.db.before-restore-<time>."
