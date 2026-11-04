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
# The database being replaced is kept on the data volume as inkcast.db.before-restore-<time> until the daily backup
# removes it, INKCAST_BACKUP_KEEP days on; its erasure records go into the restored copy, and the server erases those
# people again as it starts, before it serves (the runbook says what that covers and what it can't). KUBECTL=oc works too.
#
# The server and the restore Job never run at the same time. The script waits RESTORE_TIMEOUT seconds (default 300,
# at most a day) for the Job, and the restore gets that same moment as its deadline (`--not-after`): it never swaps
# the database once the script has given up on it. A Job that has not completed by then, or that failed, or that is
# still there when the script is interrupted, is stopped before the server is scaled back up: a running restore is
# asked to stop (a SIGTERM, which it honours before its swap and ignores after; sent through `exec`, as PID 1 drops
# the kubelet's until the handler is up, and a deleted pod takes its log with it), given RESTORE_STOP_WAIT seconds
# (default 30, at most 60) to end on its own, and its log is read before the Job and its pod are deleted. That log
# makes the last message (issue #128): "the database was replaced" when the restore says it swapped (restore.done),
# "nothing was replaced" when it says it stopped or refused, and neither when it said nothing (the runbook says how
# to tell then). If the pod cannot be confirmed gone, the server stays at 0 replicas and the script says so.
set -euo pipefail
ns="${1:?usage: restore.sh <namespace> [backup-file-name]}"
which_backup="${2:-}"
k="${KUBECTL:-kubectl}"
timeout="${RESTORE_TIMEOUT:-300}"
stop_wait="${RESTORE_STOP_WAIT:-30}"

seconds() {
  # $1 names the variable, $2 is its value, $3 the most it may be: a whole number of seconds from 1 to $3. More than
  # five digits is refused before any comparison, which `[` could not make of a number past its range.
  case "$2" in
    ''|*[!0-9]*|0*|??????*) ;;
    *) [ "$2" -le "$3" ] && return 0 ;;
  esac
  echo "$1 must be a whole number of seconds from 1 to $3 (got '$2')." >&2
  exit 2
}
seconds RESTORE_TIMEOUT "$timeout" 86400
seconds RESTORE_STOP_WAIT "$stop_wait" 60
listing=false
if [ "$which_backup" = "--list" ]; then
  listing=true
elif [ -n "$which_backup" ]; then
  # The name goes into the Job's YAML: only a backup's own shape (inkcast-<time>.db, as --list prints them) does.
  case "$which_backup" in
    inkcast-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.db) ;;
    *) echo "'$which_backup' is not a backup's name (inkcast-<time>.db, as --list shows them)." >&2; exit 2 ;;
  esac
fi
job="inkcast-restore-$(date -u +%Y%m%d%H%M%S)"

image="$($k -n "$ns" get cronjob inkcast-backup -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')"
policy="$($k -n "$ns" get cronjob inkcast-backup -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].imagePullPolicy}')"
security="$($k -n "$ns" get cronjob inkcast-backup -o jsonpath='{.spec.jobTemplate.spec.template.spec.securityContext}')"
[ -n "$security" ] || security='{"runAsNonRoot": true, "seccompProfile": {"type": "RuntimeDefault"}}'
[ -n "$policy" ] || policy=IfNotPresent
replicas="$($k -n "$ns" get deploy inkcast -o jsonpath='{.spec.replicas}')"
if $listing; then job="inkcast-backups-$(date -u +%Y%m%d%H%M%S)"; fi

if ! $listing; then
  # A restore already running (another terminal, this second or any other) would share this one's scale-down and
  # scale-up, and a Job's name within the second: refuse before touching anything. A list that fails refuses too.
  running="$($k -n "$ns" get job -o jsonpath='{range .items[?(@.status.active>0)]}{.metadata.name}{"\n"}{end}')" ||
    { echo "Could not list the namespace's jobs: not restoring beside what may be running." >&2; exit 1; }
  while read -r name; do
    case "$name" in
      inkcast-restore-*)
        echo "A restore is already running (job $name): wait for it, or remove it, before starting another." >&2
        exit 1 ;;
    esac
  done <<< "$running"
fi

# What the script has done so far, for the exit trap: whether it scaled the server to 0, whether the Job exists,
# whether the Job completed, and whether the script reached its end (then the trap has nothing to do).
scaled_down=false
applied=false
completed=false
finished=false

job_condition() {
  # "True" when the Job's condition $1 (Complete, Failed) is set; empty otherwise, or when it cannot be read.
  $k -n "$ns" get job "$job" -o jsonpath="{.status.conditions[?(@.type==\"$1\")].status}" 2>/dev/null || true
}

job_ended() {
  [ "$(job_condition Complete)" = "True" ] || [ "$(job_condition Failed)" = "True" ]
}

await_end() {
  # Until the Job is Complete or Failed (0), or $1 seconds have passed (1). Read every second: a read that fails is
  # "not yet", never a reason to give up.
  local until=$(( $(date +%s) + $1 ))
  while ! job_ended; do
    [ "$(date +%s)" -lt "$until" ] || return 1
    sleep 1
  done
}

stop_job() {
  # Stop the Job and say what became of the database, from the restore's own log, then delete the Job with its pod
  # (foreground cascade: the Job goes only after its pod has) and confirm nothing of it is left. Returns 1 if a pod
  # or the Job is still there: the server must not come back then, and nothing is said about the database (a pod
  # that is still there can still swap it).
  if ! job_ended; then
    # A running restore honours a SIGTERM before its swap (it stops within one step of its copy and says so) and
    # ignores one after (then it says it swapped). Sent through the pod's own python: `inkcast` is PID 1, which
    # drops the kubelet's SIGTERM until the handler is up, and a deleted pod takes its log, the evidence, with it.
    echo "Asking the restore to stop, then up to $stop_wait s for job $job to end."
    if $k -n "$ns" exec "job/$job" -- python -c 'import os, signal; os.kill(1, signal.SIGTERM)'; then
      await_end "$stop_wait" || echo "Job $job did not end within $stop_wait s." >&2
    else
      echo "Could not signal the restore (its pod may not have started): deleting the job." >&2
    fi
    if [ "$(job_condition Complete)" = "True" ]; then
      echo "The restore job completed as it was asked to stop: the database was replaced." >&2
      completed=true
      applied=false
      return 0
    fi
  fi
  local words
  words="$($k -n "$ns" logs "job/$job" 2>/dev/null)" || words=""
  [ -z "$words" ] || printf '%s\n' "$words"
  echo "Deleting job $job and its pod."
  $k -n "$ns" delete job "$job" --cascade=foreground --wait=true --timeout=120s --ignore-not-found || true
  $k -n "$ns" wait --for=delete pod -l "job-name=$job" --timeout=60s 2>/dev/null || true
  local left
  left="$($k -n "$ns" get pod -l "job-name=$job" -o name)" || left="(could not list the pods of job $job)"
  # A Job still there (its delete refused or failed) can still start a pod: it counts as left too.
  [ -n "$left" ] || left="$($k -n "$ns" get job "$job" -o name --ignore-not-found)" || left="(could not read job $job)"
  if [ -n "$left" ]; then
    echo "Job $job or its pod is still there ($left): the server stays at 0 replicas, so the restore cannot swap" \
         "the database under it. Remove it, then scale the server back: $k -n $ns scale deploy/inkcast" \
         "--replicas=$replicas" >&2
    return 1
  fi
  applied=false               # only now: interrupted before this, the exit trap looks again
  # The restore's own account decides (issue #128): the Job's conditions can't, as a stopped Job is deleted before
  # the controller records how its pod ended, and a restore killed as it ended may have swapped without a word.
  case "$words" in
    *'"event": "restore.done"'*)
      echo "The restore had swapped the database before the job was stopped: the database was replaced." >&2 ;;
    *'"event": "restore.stopped"'*|*'"event": "restore.refused"'*)
      echo "The restore job was stopped before the swap: nothing was replaced." >&2 ;;
    *)
      echo "The restore job was stopped, and its log does not say whether it had swapped the database: the server" \
           "comes back on whichever database is at its path. The runbook says how to tell." >&2 ;;
  esac
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
  # A second Ctrl-C ends this clean-up at once: say what it leaves, and how to undo it, rather than nothing.
  again="Interrupted again: the server may be at 0 replicas (bring it back: $k -n $ns scale deploy/inkcast"
  again="$again --replicas=$replicas), and job $job may still be there (remove it first: $k -n $ns delete job $job)."
  trap 'echo "$again" >&2; exit 130' INT TERM
  echo "restore.sh stopped before it finished." >&2
  if $completed && ! $listing; then
    echo "The restore job had completed: the database was replaced." >&2
  elif $applied && ! $listing; then
    if [ "$(job_condition Complete)" = "True" ]; then
      echo "The restore job had already completed: the database was replaced." >&2
    elif ! stop_job; then
      exit "$rc"
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

# --force: this script has just scaled the server to 0 and waited for its pod to go, so the scheduler's last
# heartbeat (seconds old) is not a running server. By hand, `inkcast restore` keeps that guard. The deadline is the
# wait's end, on this clock: the restore refuses to swap past it, so a restore that started late or ran long never
# swaps once the script has given up on it (the pod's clock must agree with this one to the second, as it does
# with time kept by NTP on both).
deadline=$(( $(date +%s) + timeout ))
if $listing; then args='["backups", "--to", "/backups"]'
elif [ -n "$which_backup" ]; then args="[\"restore\", \"/backups/$which_backup\", \"--force\", \"--not-after\", \"$deadline\"]"
else args="[\"restore\", \"--latest\", \"/backups\", \"--force\", \"--not-after\", \"$deadline\"]"; fi

echo "Restoring with $image."
applied=true
# activeDeadlineSeconds is the backstop for a script that died: this script stops the Job itself at the deadline and
# reads its log first, which the controller's own stop (a deleted pod) would take away. The grace period is for a
# restore that does not stop by itself: one that honours the stop ends within a step of its copy, well under it.
$k -n "$ns" apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $job
  labels: {app.kubernetes.io/name: inkcast, app.kubernetes.io/component: backup}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $(( timeout + 120 ))
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels: {app.kubernetes.io/name: inkcast, app.kubernetes.io/component: backup}
    spec:
      restartPolicy: Never
      terminationGracePeriodSeconds: 30
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
# When it gives up, the condition is read once more (the Job may have completed in that instant), then Failed (then
# there is nothing to wait for); otherwise, until the deadline, the wait is re-armed with the time left: a wait that
# ended early (a watch error, an API blip) is not a timeout, and must not stop a healthy restore.
remaining=$timeout
while :; do
  if $k -n "$ns" wait --for=condition=complete "job/$job" --timeout="${remaining}s" ||
     [ "$(job_condition Complete)" = "True" ]; then completed=true; applied=false; break; fi
  [ "$(job_condition Failed)" != "True" ] || break
  sleep 1
  remaining=$(( deadline - $(date +%s) ))
  [ "$remaining" -gt 0 ] || break
done
if $listing; then
  $k -n "$ns" logs "job/$job" || true
  finished=true
  $completed && exit 0
  exit 1
fi

if ! $completed; then
  if [ "$(job_condition Failed)" = "True" ]; then
    echo "The restore job failed (its log follows)." >&2
  else
    echo "The restore job did not complete within $timeout s: stopping it before the server comes back." >&2
  fi
  # Either way the Job and its pod go before the server does anything, so no late restore can swap the database
  # under it. If the pod will not go, the server stays down (stop_job says so) and this exits without scaling up.
  if ! stop_job; then finished=true; exit 1; fi
  if ! $completed; then             # stop_job found it completed as it was asked to stop: a restore after all
    scale_back
    finished=true
    exit 1
  fi
fi

$k -n "$ns" logs "job/$job" || true
scale_back
finished=true
if ! $k -n "$ns" rollout status deploy/inkcast --timeout=180s; then
  echo "The database was replaced, but the server's rollout did not finish within 180 s: check its pods" \
       "($k -n $ns get pod -l app.kubernetes.io/component=server) and its log." >&2
  exit 1
fi
echo "Restored. The previous database is on the data volume as inkcast.db.before-restore-<time>, until the daily" \
     "backup removes it, INKCAST_BACKUP_KEEP days on."
