#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The restore drill (E7.F7.6.S1): prove a backup can be restored, end to end, in a namespace running the `backup`
# component. CI runs it on kind for every change; run it on staging after any storage change.
#
#   scripts/ops/backup-drill.sh <namespace>
#
# 1. write a marker row into the live database;  2. take a backup (a Job from the CronJob);
# 3. delete the marker (the loss);  4. restore the newest backup (scripts/ops/restore.sh);
# 5. the marker must be back and the server ready. Any step failing fails the drill.
set -euo pipefail
ns="${1:?usage: backup-drill.sh <namespace>}"
k="${KUBECTL:-kubectl}"
here="$(cd "$(dirname "$0")" && pwd)"
marker="drill-$(date -u +%Y%m%d%H%M%S)"
sql() {  # one statement against the live database, through the server's own Python
  $k -n "$ns" exec deploy/inkcast -- python -c "import sqlite3,sys; c=sqlite3.connect('/data/inkcast.db'); r=c.execute(sys.argv[1], sys.argv[2:]).fetchall(); c.commit(); print(r[0][0] if r else '')" "$@"
}

echo "1. marker $marker"
sql "INSERT INTO heartbeats (name, at) VALUES (?, 1)" "$marker"
test "$(sql "SELECT COUNT(*) FROM heartbeats WHERE name = ?" "$marker")" = 1

echo "2. backup"
$k -n "$ns" create job "drill-backup-${marker#drill-}" --from=cronjob/inkcast-backup
$k -n "$ns" wait --for=condition=complete "job/drill-backup-${marker#drill-}" --timeout=300s
$k -n "$ns" logs "job/drill-backup-${marker#drill-}" | grep '"event": "backup.done"'

echo "3. the loss"
sql "DELETE FROM heartbeats WHERE name = ?" "$marker"
test "$(sql "SELECT COUNT(*) FROM heartbeats WHERE name = ?" "$marker")" = 0

echo "4. restore"
KUBECTL="$k" "$here/restore.sh" "$ns"

echo "5. check"
found="$(sql "SELECT COUNT(*) FROM heartbeats WHERE name = ?" "$marker")"
if [ "$found" != 1 ]; then
  echo "DRILL FAILED: the marker is not back after the restore." >&2
  exit 1
fi
echo "Drill passed: backed up, lost, restored, and the server is ready on the restored database."
