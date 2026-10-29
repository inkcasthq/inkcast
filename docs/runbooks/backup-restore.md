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
| Copies kept | 14, on the separate volume `inkcast-backups` (`INKCAST_BACKUP_KEEP`). A person who deletes their account is erased from the database at once, but a backup taken before that still holds their data until it ages out, within this many days; the deletion pages tell them so, with this number, so the server reads `INKCAST_BACKUP_KEEP` too (set it in the shared ConfigMap when the CronJob's value changes) |
| Alert | [InkcastBackupMissing](alerts.md#inkcastbackupmissing): no successful backup for 26 hours (critical) |
| Drill | `scripts/ops/backup-drill.sh`, run by CI on kind for every change |
| Recovery point | Up to 24 hours (one backup a day); take one before any risky change |

## How a backup works

`inkcast backup` copies the database with SQLite's online backup API, so it is consistent while the server keeps serving, uncommitted WAL pages included.

The copy only counts if all of these hold:
1. it passes `PRAGMA integrity_check`;
2. it is renamed into place from a temporary name;
3. a manifest is written beside it (`inkcast-<time>.json`, with the size, SHA-256 and the row count of every table).

Retention removes the oldest copies, and only after a good backup. A failed backup removes nothing.

**A backup is as sensitive as the database.** Platform credentials inside it are encrypted with `INKCAST_SECRET_KEY`, so the backup is useless without that key, and the two must never be stored together. Files are written `0640`, and the backup pod has no network at all (its NetworkPolicy denies everything).

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
2. Runs `inkcast restore` as a one-off Job, with the same image and both volumes, and waits up to `RESTORE_TIMEOUT` seconds for it (default 300; the Job gets the same number as its `activeDeadlineSeconds`, so it cannot outlive the wait). The restore:
   - verifies the backup (checksum against the manifest, then the integrity check);
   - copies it in;
   - checks the copy's row counts against the manifest;
   - carries the erasure records (`user_erasures`: a salted hash per person who deleted their account, the time, the counts; nothing that names them) and their aliases (`user_erasure_aliases`: the other salted hash an erasure is known by after a rotation of `INKCAST_SECRET_KEY`) of the database being replaced into the copy, skipping any it already has (`restore.erasures.carried` in the Job's log, with both counts; nothing is logged when there is nothing to carry). The record alone names nobody, but the person's row carries the same hash (`users.erasure_hash`), so whoever holds the database and a backup from before the erasure can tell which record is theirs; that backup holds their whole row anyway, so the hash tells them nothing more;
   - only then swaps it in.
3. Once the Job's `Complete` condition is set, and only then, scales the server back up and waits for it to be ready. **Before it serves anything, the server re-applies the erasures:** when a process opens the store it looks up, through an index, everyone whose stored hash (`users.erasure_hash`, written when the person was created) is in `user_erasures` but who has rows, which is what a backup taken before their deletion brings back, and erases them again. One log line per person, `identity.erasure.reapplied`, with the id and the number of rows; no second audit row, and `inkcast_identity_users_deleted_total` does not move (it was one deletion). The step costs the records and the matches, not the number of users; with nothing to do, one read. A backup from before the hash column existed is filled in once, at that first open (one hash per person, in batches), at the one-off cost described under [Upgrading to the hash column](#upgrading-to-the-hash-column). Check the server's log after the rollout: `oc -n "$NS" logs deploy/inkcast | grep identity.erasure.reapplied`.

Nothing is deleted. The replaced database stays on the data volume as `inkcast.db.before-restore-<time>`, with its WAL files.

**The server and the restore Job never run at the same time.** The script reports "Restored" only when the Job completed. On every other path it removes the Job and its pod (`kubectl delete job --cascade=foreground --wait`) and confirms no pod of the Job is left *before* it scales the server back up, then exits non-zero saying nothing was replaced:
- the Job failed (its log is printed first; a failed Job is removed rather than left for its `ttlSecondsAfterFinished`);
- the Job did not complete within `RESTORE_TIMEOUT` seconds (a very large `user_erasures` table, a slow volume): the deadline stops it on the cluster side, and the script deletes it on its own;
- the script was interrupted (Ctrl-C, a lost terminal) or a step failed after the Job was created: an exit trap does the same, unless the Job had already completed, in which case it says the database was replaced and brings the server back.

If the Job or a pod of it will not go (the delete was refused or timed out, or the list cannot be read), the server **stays at 0 replicas** and the script says so: bringing it back beside a restore that may still swap the database is the one thing it must not do. Remove the leftover Job and its pod by hand (`oc -n "$NS" delete job <name>`), then `oc -n "$NS" scale deploy/inkcast --replicas=<n>` with the count the script printed when it scaled down. A restore that needs longer than 300 s runs with `RESTORE_TIMEOUT=900 KUBECTL=oc scripts/ops/restore.sh "$NS"`.

After a restore:
- **Tokens and sessions** issued after the backup no longer work. People sign in again, and CI tokens created since must be re-created.
- **Posts published after the backup** are not in the ledger. The next Git sync asks each platform before creating anything (`find_existing`), so nothing is posted twice. Posts on platforms without that check are reported `unconfirmed` for a person to resolve.
- **Platform connections** made after the backup must be reconnected.
- **People who deleted their account** after the backup was taken are erased again by the server at start-up, as above, from the records the restore carried over.

**Limitation: an erasure that only the lost database knew of.** A backup is a snapshot of the whole file, so a backup taken before a deletion holds the person's rows and not the record of their erasure, and one taken after holds the record and no rows. The record that re-applies an erasure therefore comes from the database being replaced. When that database is gone too (the data volume was lost, or the file was unreadable and moved aside by hand) and the backup restored is older than a deletion, nothing in the restored file says that person asked to be erased, and the start-up step cannot know. What covers it is the Keycloak user sync, which "Delete my account" needs anyway: its first pass after start-up re-reads the `DELETE_ACCOUNT` events since the cursor the backup holds (which predates the deletion) and reconciles every Keycloak-backed person against Keycloak, so a person who is gone there with a `DELETE_ACCOUNT` event is erased (`identity.user.deleted`, `via=event`) within the first pass, seconds after the server is up; one an administrator deleted is cut off (`identity.user.disabled`). The gap that remains is a `DELETE_ACCOUNT` event older than the realm's user-event retention: the shipped realm keeps events 7 days (`eventsExpiration` in `deploy/keycloak/realm-inkcast.json`) and backups 14, so for a restore of a backup more than 7 days old the reconcile can only cut such a person off, not erase them. To close it, set the realm's event expiration to at least `INKCAST_BACKUP_KEEP` days, or restore a backup newer than the oldest deletion you know of. Either way, after such a restore watch the first sync pass finish (`identity.user.deleted` or `identity.user.disabled` per person) before telling anyone the restore is done.

## Upgrading to the hash column

The first start on an image with `users.erasure_hash` (issue #113) fills the hash of every existing user before `/livez` answers: in batches of 10,000 rows, each its own transaction, which holds the write lock for a fraction of a second (longer as the table grows: about a quarter of a second per batch at a million users). **The one-off cost is about 10 to 25 seconds per million users** (measured; a slow volume or a busy node takes longer). The same fill runs after a restore of a backup from before the column, and in split mode every service pod runs it at the same time, sharing the work. Nothing serves until it is done, and nobody is erased by it: it writes one hash per person and nothing else.

The log says how far it has got, one line per batch (`identity.erasure_hash.backfill`: the rows filled so far, the rows without a hash when that process started, the batches), and one line when every user has a hash (`identity.erasure_hash.backfilled`). A start with nothing to fill logs nothing.

**A restart in the middle is safe.** Each batch commits on its own, so a pod stopped mid-fill keeps the batches it committed, and the next start fills what is left and counts from there; nothing is filled twice and nothing is lost. The startup probe allows 60 seconds (`failureThreshold` 30 at `periodSeconds` 2), which a database of several million users, or a slow node, can outlast: the pod is then restarted mid-fill, and converges, since every start gets a minute's worth of batches further. Expect [InkcastPodRestarting](alerts.md#inkcastpodrestarting) only if that takes more than three restarts; raise the startup probe's `failureThreshold` for that one rollout if it does.

## By hand (outside Kubernetes)

```bash
INKCAST_DATABASE=./inkcast.db inkcast backup --to ./backups --keep 14
inkcast backups --to ./backups
inkcast restore --latest ./backups      # refuses while the server's heartbeat is under 2 minutes old
```

The same rules apply: the restore carries the erasure records of the database it replaces, and the server re-applies them when it next starts, before it serves.

## Off-cluster copies

`inkcast-backups` lives in the same cluster as the data, so losing the whole cluster loses both. Protect it further with either:
- volume snapshots of `inkcast-backups` replicated by the storage platform (OpenShift Data Foundation, a CSI driver with snapshot classes); or
- a Velero schedule for the namespace.

Story E7.F7.6.S2 covers object-storage replication.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
