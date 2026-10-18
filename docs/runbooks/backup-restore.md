# Backup and restore (SQLite)

Until PostgreSQL arrives in E3 (CloudNativePG: continuous WAL archiving and point-in-time recovery, story F3.1.S5), Inkcast keeps its data in one SQLite file on the `inkcast-data` volume. That file holds:
- users;
- token hashes;
- encrypted platform credentials;
- posts and the publish ledger.

This runbook keeps that file safe.

| What | Where |
|---|---|
| Daily backup | The `backup` component, on by default in the OpenShift, Kubernetes and CI overlays: CronJob `inkcast-backup`, 03:17 UTC |
| Copies kept | 14, on the separate volume `inkcast-backups` (`INKCAST_BACKUP_KEEP`) |
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
oc -n inkcast create job inkcast-backup-now --from=cronjob/inkcast-backup
oc -n inkcast logs job/inkcast-backup-now        # look for "event": "backup.done"
```

## List the backups

```bash
KUBECTL=oc scripts/ops/restore.sh inkcast --list
```

Each line shows the name, size, checksum, and whether the backup still matches its manifest.

## Restore

```bash
KUBECTL=oc scripts/ops/restore.sh inkcast                              # the newest backup
KUBECTL=oc scripts/ops/restore.sh inkcast inkcast-20261018T031700Z.db  # a chosen one
```

The script does the following, stopping at the first failure:
1. Scales the server to 0, because a restore must not run under a live server.
2. Runs `inkcast restore` as a one-off Job, with the same image and both volumes. The restore:
   - verifies the backup (checksum against the manifest, then the integrity check);
   - copies it in;
   - checks the copy's row counts against the manifest;
   - only then swaps it in.
3. Scales the server back up and waits for it to be ready.

Nothing is deleted. The replaced database stays on the data volume as `inkcast.db.before-restore-<time>`, with its WAL files. If the restore Job fails, the server comes back on the database it had.

After a restore:
- **Tokens and sessions** issued after the backup no longer work. People sign in again, and CI tokens created since must be re-created.
- **Posts published after the backup** are not in the ledger. The next Git sync asks each platform before creating anything (`find_existing`), so nothing is posted twice. Posts on platforms without that check are reported `unconfirmed` for a person to resolve.
- **Platform connections** made after the backup must be reconnected.

## By hand (outside Kubernetes)

```bash
INKCAST_DATABASE=./inkcast.db inkcast backup --to ./backups --keep 14
inkcast backups --to ./backups
inkcast restore --latest ./backups      # refuses while the server's heartbeat is under 2 minutes old
```

## Off-cluster copies

`inkcast-backups` lives in the same cluster as the data, so losing the whole cluster loses both. Protect it further with either:
- volume snapshots of `inkcast-backups` replicated by the storage platform (OpenShift Data Foundation, a CSI driver with snapshot classes); or
- a Velero schedule for the namespace.

Story E7.F7.6.S2 covers object-storage replication.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
