# ADR-0014: PostgreSQL on CloudNativePG

**Status:** accepted, 2026-10-18 (owner) · **Epic:** E3.F3.1 (PostgreSQL repositories), with E3.F3.1.S5 (backups) and E7.F7.6 (DR)

## Context

Inkcast keeps everything in one SQLite file on one ReadWriteOnce volume. That allows:
- one replica only;
- Recreate deployments;
- daily file backups at best (the stopgap in `components/backup`).

E3 needs:
- several replicas per service;
- row-level security as defence in depth behind OpenFGA;
- a database for OpenFGA's own store;
- backups with a recovery point of minutes, not a day.

The operator has to run on OpenShift (restricted-v2) and plain Kubernetes, air-gapped, without a cloud database.

## Decision

**PostgreSQL, run by the CloudNativePG operator.**

What CloudNativePG is:
- Apache-2.0 licensed and a CNCF Sandbox project; EDB originally built and donated it.
- Inkcast deploys it as a separate operator and doesn't redistribute its code, so it sits beside PolyForm Shield without conflict.

How Inkcast uses it:
- **Clusters:**
  - one `Cluster` for Inkcast;
  - one for OpenFGA, or a separate database in the same cluster for small installs;
  - 3 instances in production (1 primary, 2 replicas, synchronous quorum optional), 1 in CI and dev.
- **Backups (E3.F3.1.S5):**
  - scheduled base backups plus continuous WAL archiving to S3-compatible object storage (ODF/NooBaa, MinIO, or a cloud bucket);
  - point-in-time recovery;
  - a restore drill in CI that restores into a new `Cluster` and runs the end-to-end suite;
  - targets: recovery point 5 minutes, recovery time 30 minutes.
- **Connections:**
  - through CloudNativePG's PgBouncer `Pooler`;
  - credentials from the Secret the operator creates, read through the secrets design (`_FILE`, `SecretValue`);
  - TLS on, with the operator's CA.
- **Network:** an egress rule for 5432 to the cluster's pods (namespace and pod selectors), filed as E3.F3.10.S3.
- **Monitoring:**
  - the operator's PodMonitor and its default alert rules;
  - the backing-services dashboard (E7.F7.7.S3);
  - `InkcastBackupMissing` is replaced by the operator's backup alerts.
- **DR (E7.F7.6.S3):** an active/passive replica cluster in a second region; the scheduler is active in one region only.

## Options considered

| Option | Why not |
|---|---|
| Stay on SQLite | Single replica, no row-level security, file-level backups only |
| Crunchy Postgres Operator (PGO) | A good operator, also Apache-2.0. CloudNativePG needs no StatefulSets or sidecar Patroni, is in the CNCF and is what the architecture already names; either would do, and the repositories stay operator-neutral (plain PostgreSQL URLs) |
| Zalando postgres-operator | Patroni-based and older in design; weaker OpenShift story |
| Managed PostgreSQL (RDS, Cloud SQL, Azure) | Not available air-gapped or on-premises; remains possible for SaaS through the same connection settings |
| A StatefulSet by hand | Failover, backups and upgrades become our code |

## Consequences

- The repositories move from SQLite to PostgreSQL behind the same interfaces (`libs/db`), with migrations. SQLite stays for local development and the single-file self-host until it's retired.
- `components/backup` and `scripts/ops/restore.sh` are the stopgap until then. They are removed when CloudNativePG backups are in place and drilled.
- Air-gapped installs mirror the operator and its PostgreSQL images (`oc-mirror` or `skopeo`), like every other image.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
