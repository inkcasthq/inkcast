# Backup and restore (SQLite)

Until PostgreSQL arrives in E3 (CloudNativePG: continuous WAL archiving and point-in-time recovery, story F3.1.S5), Inkcast keeps its data in one SQLite file on the `inkcast-data` volume. That file holds:
- users;
- token hashes;
- encrypted platform credentials;
- posts and the publish ledger.

This runbook keeps that file safe.

**Namespace.** The commands below write the app's namespace as `$NS`. Set it once before you start: `NS=inkcast` for a self-host from `deploy/kustomize/overlays/openshift` or `overlays/kubernetes`, `NS=inkcast-staging` for `deploy/kustomize/environments/staging`, and `NS=inkcast-prod` for `deploy/kustomize/environments/production`.

| What | Where |
|---|---|
| Daily backup | The `backup` component, on by default in the OpenShift, Kubernetes and CI overlays: CronJob `inkcast-backup`, 03:17 UTC |
| Retention | `INKCAST_BACKUP_KEEP` days (14), on the separate volume `inkcast-backups`: a backup that many days old or older is removed by the next good backup. By age, not by count, so a backup taken by hand evicts no daily copy, and the first good backup after days without one removes everything that old at once; the days without one are the alert's. A person who deletes their account is erased from the database at once, but a backup taken before that still holds their data until it ages out, within this many days; the deletion pages tell them so, with this number, so the server and the CronJob read `INKCAST_BACKUP_KEEP` from the same ConfigMap, `inkcast-config` (unset there, both fall back to 14) |
| Alert | [InkcastBackupMissing](alerts.md#inkcastbackupmissing): no successful backup for 26 hours (critical) |
| Drill | `scripts/ops/backup-drill.sh`, run by CI on kind for every change |
| Recovery point | Up to 24 hours (one backup a day); take one before any risky change |

## How a backup works

`inkcast backup` copies the database with SQLite's online backup API, so it is consistent while the server keeps serving, uncommitted WAL pages included.

The copy only counts if all of these hold:
1. it passes `PRAGMA integrity_check`;
2. it is renamed into place from a temporary name;
3. a manifest is written beside it (`inkcast-<time>.json`, with the size, SHA-256 and the row count of every table).

Retention removes the copies `INKCAST_BACKUP_KEEP` or more days old (the day in the file's name, UTC), with what a backup killed midway left that old (`.inkcast-<time>.db.partial`, or a backup without its manifest), and only after a good backup. A failed backup removes nothing, so while the CronJob fails the older copies stay: that is what [InkcastBackupMissing](alerts.md#inkcastbackupmissing) reports, and the first good backup removes them.

**One run never removes every earlier backup.** A node whose clock jumped a year ahead would otherwise find every backup a year old and remove them all, leaving its own copy, dated a year on, as the only restore point (#405). So when the newest backup from before today has aged out (`INKCAST_BACKUP_KEEP` days old or more; more than one with `1`, which removes yesterday's copy every day), the run keeps everything (pre-restore copies included), logs `backup.retention.refused` after its `backup.done` line, and exits 1: the Job fails, and InkcastBackupMissing fires within hours. That gap means one of two things, and the files cannot tell them apart: the node's clock is `INKCAST_BACKUP_KEEP` - 1 days ahead or more (check it, `date -u` on the node against a trusted source; nothing else is needed, the backup is good), or no backup succeeded for the whole retention window (the alert has been firing for as long; nothing is needed either: the next good backup finds a gap of one day and removes them). A copy a wrong clock dated after today stays a restore point, but ages out with the day its run followed (`previous` in its manifest: the newest backup there was), `INKCAST_BACKUP_KEEP` days after that day rather than a year on, and `--latest` never picks it over a copy of a sane day. What one refusal cannot cover is a clock still wrong at the next day's run, which finds the first run's copy a day old and sane: the failed Job is the day to fix the clock.

**A backup is as sensitive as the database.** Platform credentials inside it are encrypted with `INKCAST_SECRET_KEY`, so the backup is useless without that key, and the two must never be stored together. Files are written `0640`, and the backup pod has no network at all (its NetworkPolicy denies everything). Deleted content is overwritten in the database (`PRAGMA secure_delete`, set by every connection the server opens), so a backup taken after an erasure carries none of the erased rows' bytes in its free space; one taken before holds them whole, until it ages out.

## Take one now

```bash
oc -n "$NS" create job inkcast-backup-now --from=cronjob/inkcast-backup
oc -n "$NS" logs job/inkcast-backup-now        # look for "event": "backup.done"
```

## List the backups

```bash
KUBECTL=oc scripts/ops/restore.sh "$NS" --list
```

Each line shows the name, size, checksum, and whether the backup still matches its manifest.

## Restore

```bash
KUBECTL=oc scripts/ops/restore.sh "$NS"                              # the newest backup
KUBECTL=oc scripts/ops/restore.sh "$NS" inkcast-20261018T031700Z.db  # a chosen one
```

The script does the following, stopping at the first failure:
1. Scales the server to 0, because a restore must not run under a live server.
2. Runs `inkcast restore` as a one-off Job, with the same image and both volumes, and waits up to `RESTORE_TIMEOUT` seconds for it (default 300, at most 86400). The restore gets the end of that wait as its own deadline (`--not-after`, seconds since the epoch on the script's clock): past it, it stops rather than swaps, so a restore that started late or ran long never swaps once the script has given up on it. The Job's own `activeDeadlineSeconds` is two minutes later: a backstop for a script that died, as a living one stops the Job itself. The restore:
   - verifies the backup (checksum against the manifest, then the integrity check);
   - copies it in, in steps of 4 MiB, checking between two whether it was asked to stop or its deadline passed;
   - checks the copy's row counts against the manifest;
   - carries the erasure records (`user_erasures`: a salted hash per person who deleted their account, the time, the counts; nothing that names them) and their aliases (`user_erasure_aliases`: the other salted hash an erasure is known by after a rotation of `INKCAST_SECRET_KEY`) of the database being replaced into the copy, skipping any it already has (`restore.erasures.carried` in the Job's log, with both counts; nothing is logged when there is nothing to carry). The record alone names nobody, but the person's row carries the same hash (`users.erasure_hash`), so whoever holds the database and a backup from before the erasure can tell which record is theirs; that backup holds their whole row anyway, so the hash tells them nothing more;
   - checks once more that it was not asked to stop and its deadline has not passed, and only then swaps it in. Past that check the swap goes through whatever arrives.
3. Once the Job's `Complete` condition is set, and only then, scales the server back up and waits for it to be ready. **Before it serves anything, the server re-applies the erasures:** when a process opens the store it looks up, through an index, everyone whose stored hash (`users.erasure_hash`, written when the person was created) is in `user_erasures` but who has rows, which is what a backup taken before their deletion brings back, and erases them again. One log line per person, `identity.erasure.reapplied`, with the id and the number of rows; no second audit row, and `inkcast_identity_users_deleted_total` does not move (it was one deletion). The step costs the records and the matches, not the number of users; with nothing to do, one read. A backup from before the hash column existed is filled in once, at that first open (one hash per person, in batches), at the one-off cost described under [Upgrading to the hash column](#upgrading-to-the-hash-column). Check the server's log after the rollout: `oc -n "$NS" logs deploy/inkcast | grep identity.erasure.reapplied`.

Nothing is deleted by the restore. The replaced database stays on the data volume as `inkcast.db.before-restore-<time>`, with its WAL files, until it ages out: it holds whoever is erased after the restore, so the daily backup removes it, with those files, once it is `INKCAST_BACKUP_KEEP` or more days old (`removed_copies` in the `backup.done` line), the same rule as the backups. It is there to look into a restore that went wrong, not to restore from: `inkcast restore` only takes a backup with its manifest, and putting the copy back by hand would skip the carry of erasure records above. To go back to it within those days: scale the server to 0, run `inkcast backup --database /data/inkcast.db.before-restore-<time> --to /backups` as a Job with both volumes (the restore Job above, with `/backups` writable), then restore that backup. Its name carries the copy's time, not today's, so it ages out when the copy would have (a copy already that old is refused).

**The server and the restore Job never run at the same time.** The script reports "Restored" only when the Job completed. On every other path it stops the Job, reads its log, removes the Job and its pod (`kubectl delete job --cascade=foreground --wait`) and confirms no pod of the Job is left *before* it scales the server back up, then exits non-zero:
- the Job failed (a failed Job is removed rather than left for its `ttlSecondsAfterFinished`);
- the Job did not complete within `RESTORE_TIMEOUT` seconds (a very large `user_erasures` table, a slow volume). A `kubectl wait` that ends early for another reason (a watch error, an API blip) is re-armed until the deadline, not taken for the timeout;
- the script was interrupted (Ctrl-C, a lost terminal) or a step failed after the Job was created: an exit trap does the same, unless the Job had already completed, in which case it says the database was replaced and brings the server back. A second Ctrl-C during that clean-up ends it at once; the script prints what it leaves (the server at 0, the Job) and the two commands that undo it.

**What the last message says comes from the restore's own log, never from a guess.** `inkcast` is PID 1 in the image, and a PID 1 drops any signal it has no handler for: before #128 a restore ignored the SIGTERM the kubelet sent when its pod was deleted, ran on through the pod's grace period, and could finish its swap while the script said "Nothing was replaced". Now the restore handles SIGTERM (and Ctrl-C): a stop before its last check ends it within one step of the copy, logs `restore.stopped` and leaves the old database and no staging copy; one after that check changes nothing, and it logs `restore.done`. The script stops a running restore by sending that SIGTERM through `kubectl exec` (a deleted pod would take its log with it), gives the Job `RESTORE_STOP_WAIT` seconds (default 30, at most 60) to end on its own, reads the log, and only then deletes the Job. It then says one of:
- `the database was replaced`: the log says `restore.done` (or the Job completed as it was asked to stop). The server comes back on the restored database, and the previous one is on the data volume under its `before-restore` name;
- `nothing was replaced`: the log says `restore.stopped` or `restore.refused`. The server comes back on the database it had;
- `its log does not say whether it had swapped the database`: the restore was killed before it could say (the pod's grace period of 30 s ran out: a restore wedged on its volume), or its log could not be read. The server comes back on whichever database is at its path. To tell which, once the server is up: `oc -n "$NS" exec deploy/inkcast -- ls -li /data`. The swap's first step hard-links the current database to its `before-restore` name and its last step renames the restored copy over it, so look for a `before-restore` copy stamped at or after the time in the job's name (`inkcast-restore-<time>`, UTC, as the script printed it), not just the newest copy, which can be an earlier restore's: no such copy, or one sharing `inkcast.db`'s inode (a link count of 2), means the swap did not happen; one on another inode means it did. A restore killed before its swap leaves its staging copy, `.inkcast.db.restoring` (a whole copy of the backup, with the erasure records carried into it), beside the database: nothing reads it, and the next daily backup removes it, with its journal, when no restore holds the data directory's lock (`restore.staging.removed` in the backup's log, with the bytes); a backup that finds the lock held leaves it to the restore that holds it (#404).

If the Job or a pod of it will not go (the delete was refused or timed out, or the list cannot be read), the server **stays at 0 replicas** and the script says so: bringing it back beside a restore that may still swap the database is the one thing it must not do. Remove the leftover Job and its pod by hand (`oc -n "$NS" delete job <name>`), then `oc -n "$NS" scale deploy/inkcast --replicas=<n>` with the count the script printed when it scaled down. A restore that needs longer than 300 s runs with `RESTORE_TIMEOUT=900 KUBECTL=oc scripts/ops/restore.sh "$NS"`. A restore Job already running in the namespace (another terminal) makes the script refuse before it touches the server, and a backup's name must have a backup's shape (`inkcast-<time>.db`, as `--list` prints them).

After a restore:
- **Tokens and sessions** issued after the backup no longer work. People sign in again, and CI tokens created since must be re-created.
- **Posts published after the backup** are not in the ledger. The next Git sync asks each platform before creating anything (`find_existing`), so nothing is posted twice. Posts on platforms without that check are reported `unconfirmed` for a person to resolve.
- **Platform connections** made after the backup must be reconnected.
- **People who deleted their account** after the backup was taken are erased again by the server at start-up, as above, from the records the restore carried over.

**Limitation: an erasure that only the lost database knew of.** A backup is a snapshot of the whole file, so a backup taken before a deletion holds the person's rows and not the record of their erasure, and one taken after holds the record and no rows. The record that re-applies an erasure therefore comes from the database being replaced. When that database is gone too (the data volume was lost, or the file was unreadable and moved aside by hand) and the backup restored is older than a deletion, nothing in the restored file says that person asked to be erased, and the start-up step cannot know. What covers it is the Keycloak user sync, which "Delete my account" needs anyway: its first pass after start-up re-reads the `DELETE_ACCOUNT` events since the cursor the backup holds (which predates the deletion) and reconciles every Keycloak-backed person against Keycloak, so a person who is gone there with a `DELETE_ACCOUNT` event is erased (`identity.user.deleted`, `via=event`) within the first pass, seconds after the server is up; one an administrator deleted is cut off (`identity.user.disabled`). The gap that remains is a `DELETE_ACCOUNT` event older than the realm's user-event retention: the shipped realm keeps events 7 days (`eventsExpiration` in `deploy/keycloak/realm-inkcast.json`) and backups 14, so for a restore of a backup more than 7 days old the reconcile can only cut such a person off, not erase them. To close it, set the realm's event expiration to at least `INKCAST_BACKUP_KEEP` days, or restore a backup newer than the oldest deletion you know of. Either way, after such a restore watch the first sync pass finish (`identity.user.deleted` or `identity.user.disabled` per person) before telling anyone the restore is done.

## Upgrading to the hash column

The first start on an image with `users.erasure_hash` (issue #113) fills the hash of every existing user before `/livez` answers: in batches of 10,000 rows, each its own transaction, which holds the write lock for a fraction of a second (longer as the table grows: about a quarter of a second per batch at a million users). **The one-off cost is about 10 to 25 seconds per million users** (measured; a slow volume or a busy node takes longer). The same fill runs after a restore of a backup from before the column, and in split mode every service pod runs it at the same time, sharing the work. Nothing serves until it is done, and nobody is erased by it: it writes one hash per person and nothing else.

The log says how far it has got, one line per batch (`identity.erasure_hash.backfill`: the rows filled so far, the rows without a hash when that process started, the batches), and one line when every user has a hash (`identity.erasure_hash.backfilled`). A start with nothing to fill logs nothing.

**The fill and a rotated key.** The fill writes each hash under the key current at that start. The record of an erasure carries the hash stored with the person's row when they were erased, so a row restored *with* its hash matches its record whatever key is current now; a row filled now matches only records made under the current key. When the fill runs beside existing records (a restore of a backup from before the column), the server logs `identity.erasure_hash.filled_with_records` with the rows filled, the records, and how many people were re-erased at that start. If `INKCAST_SECRET_KEY` was rotated since the oldest record, the people erased before the rotation whose rows that backup brought back are matched by no record and are not re-erased by this step: the Keycloak sync's first pass covers them as the limitation above describes (erased with their `DELETE_ACCOUNT` event, cut off otherwise), so watch it finish. With no rotation since, the line is information only.

**A restart in the middle is safe.** Each batch commits on its own, so a pod stopped mid-fill keeps the batches it committed, and the next start fills what is left and counts from there; nothing is filled twice and nothing is lost. The startup probe allows 60 seconds (`failureThreshold` 30 at `periodSeconds` 2), which a database of several million users, or a slow node, can outlast: the pod is then restarted mid-fill, and converges, since every start gets a minute's worth of batches further. Expect [InkcastPodRestarting](alerts.md#inkcastpodrestarting) only if that takes more than three restarts; raise the startup probe's `failureThreshold` for that one rollout if it does.

## By hand (outside Kubernetes)

```bash
INKCAST_DATABASE=./inkcast.db inkcast backup --to ./backups --keep 14
inkcast backups --to ./backups
inkcast restore --latest ./backups      # refuses while the server's heartbeat is under 2 minutes old
```

The same rules apply: the restore carries the erasure records of the database it replaces, and the server re-applies them when it next starts, before it serves. Ctrl-C (or a SIGTERM) stops it before its swap, with `restore.stopped` and the old database in place; `--not-after <epoch>` makes it stop by itself past that time.

## Off-cluster copies

`inkcast-backups` lives in the same cluster as the data, so losing the whole cluster loses both. Protect it further with either:
- volume snapshots of `inkcast-backups` replicated by the storage platform (OpenShift Data Foundation, a CSI driver with snapshot classes); or
- a Velero schedule for the namespace.

**Their retention must be at most `INKCAST_BACKUP_KEEP` days.** A snapshot or a Velero backup is a copy outside the rotation above: nothing here removes it, and the deletion pages promise that copies of a person's data age out within `INKCAST_BACKUP_KEEP` days, so a longer retention on the snapshot schedule or the Velero schedule (`--ttl`) makes the pages understate it. Set it to that many days or fewer, and when `INKCAST_BACKUP_KEEP` changes, change it too. A snapshot of `inkcast-data` (the live database, with the copies restores leave beside it) is bound by the same days. Such a snapshot copies the write-ahead log too (`inkcast.db-wal`), which keeps every page it ever held until it is cut, a checkpoint alone only letting the writer start over at its beginning: so every erasure ends with a checkpoint that copies the zeroed pages into the database file and cuts the log to nothing (`PRAGMA wal_checkpoint(TRUNCATE)`), and housekeeping repeats it every 30 minutes, whatever its other steps did, for an erasure whose checkpoint met a reader on an older snapshot (`identity.erasure.wal_pending` in the server's log; a backup's copy is such a reader). Until a checkpoint cuts the log, a snapshot of `inkcast-data` can still hold the person's bytes in it (#404). That is not bounded by 30 minutes: housekeeping runs at the first scheduler tick 30 minutes after its last run, and only while a scheduler runs, and a reader that still holds its snapshot then defers the cut to the next run, and so on for as long as it holds. Each run that cannot cut the log logs `scheduler.housekeeping.wal_pending` in the scheduler's log, with the frame count (#470): two in a row mean the log has kept its bytes for an hour or more. End the reader that holds it: a backup Job still copying, or a process with a long read open on the volume.

Restoring from such a copy puts files back as they were. A volume restore of `inkcast-backups` brings the backups back, and `inkcast restore` from one of them carries the erasure records and the server re-erases, as above. A volume restore of `inkcast-data` puts a whole database in place with no carry: that is the lost-database case of the limitation above, so the people erased since that copy was taken are known only to Keycloak, and the first sync pass after start-up erases or cuts them off as described there.

Story E7.F7.6.S2 covers object-storage replication.

**The key apart from the data.** `secret-key`, which opens every backup here, is never copied to the same place as the backups: its copy is the cluster backup of `inkcast-secrets` into a storage location of its own, with its own bucket and its own credential ([env-secrets.md](env-secrets.md), step 1; [ADR-0020](../adr/0020-root-secret-copy-in-its-own-storage-location.md)). Whatever E7.F7.6.S2 copies `inkcast-backups` to, it is another location, never `root-secrets`.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
