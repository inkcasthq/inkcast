# Inkcast: OpenFGA behaviour that matters for our model and engines

*Research date: 2026-10-22, for issues #72 (engine parity) and #71 / PR #99 (roles scoped to their organization). Sources:*
- *the pinned code, read over git: the `fga` CLI v0.8.2 (`.github/workflows/ci.yml`, `FGA_VERSION`), whose `go.mod` embeds `github.com/openfga/openfga v1.22.0`;*
- *OpenFGA v1.22.0's `pkg/server/config/config.go`;*
- *the official docs and blog, read on that date with a web-fetch tool.*

*Anything not confirmed in source or on an official page is flagged **[UNVERIFIED]**. Disagreements between sources are flagged **[CONFLICT]**.*

---

## TL;DR

| Question | Answer (OpenFGA v1.22.0, pinned) | What Inkcast does |
|---|---|---|
| Is a userset a member of itself, e.g. `check(organization:acme#member, member, organization:acme)`? | **Yes, today.** The default check engine answers true from the model alone (`IsSelfDefining`). | The local engine matches it since PR #108, with a yaml case pinning it. |
| Will that stay? | **No.** The new "weighted graph" check engine answers **false** unless stored tuples support it. It is experimental in v1.22.0 (`weighted_graph_check`), and no changeover date is set. | The yaml case is an early warning; flip the local engine when the pinned version flips. |
| Resolution depth | `DefaultResolveNodeLimit = 25`, `DefaultResolveNodeBreadthLimit = 10`. | `MAX_DEPTH = 25`. The model peaks at about 10 levels. |
| BatchCheck | `DefaultMaxChecksPerBatchCheck = 50`; concurrency 50. | `MAX_BATCH_CHECK = 50`. `OpenFgaEngine` splits larger batches into chunks; the local engine refuses (#72). |
| Writes | `DefaultMaxTuplesPerWrite = 100`. | `MAX_TUPLES_PER_WRITE = 100`. |
| ListObjects / ListUsers | 1000 results, 3 s deadline each. | `MAX_LIST_OBJECTS = 1000`. Lists use ListObjects on workspaces, then filter in the database (ADR-0007). |
| Errors inside `or` / `and` | Today's engine can stop on an error in one union branch. The new engine tolerates errors in union branches and fails fast on intersection and exclusion. | The local engine raises on the first error; it fails closed (#72). |
| Model compatibility with the new engine | It refuses recursion through `and` or `but not`, a `rel from parent` whose `rel` is missing on an allowed type, and alias traversal in Check. | **Our model is compatible:** no recursion at all, no `but not`, and every `from` resolves (checked 2026-10-22 against #99's model). |

---

## 1. Usersets that are members of themselves

- **Today (v1 engine, the default in v1.22.0):** `check(document:A#viewer, viewer, document:A)` is true, because the relation exists in the model, not because of any tuple. OpenFGA's Go package `v2breaking` names this shape `self_referential_userset`: "v1 always returned TRUE … v2 evaluates against the schema and returns FALSE".
- **Where we met it:** PR #99 added `member_assignee: assignee and member from org` to `role`. When an organization assigns its own role to `organization:acme#member`, the `member from org` step asks whether `organization:acme#member` is a `member` of `organization:acme`. The v1 engine says yes; our local engine said no until PR #108.
- **Effect on enforcement: none.** `require()` only checks `user:<id>`. The difference shows only when a userset is checked as the subject (the model tests, and Expand-style tooling).
- **The weighted-graph engine reverses it:** "A relation existing in the model is not sufficient evidence that a group has access to an object." The same post says alias traversal in Check also stops: a tuple stored for `doc#allowed` no longer answers for an alias relation.
- **Our position:** the local engine follows the **pinned** OpenFGA version, because it stands in for the server in tests and small self-hosts. When the pin moves to a version where the weighted graph is the default, the yaml case added by #108 fails in CI's `fga model test`. At that point we flip the local engine in the same PR.

## 2. The new check engine (weighted graph)

- **Status:** ListObjects already uses it. Check is next. Models it can't handle fall back to the legacy algorithm, a fallback the post says "will be removed soon". No version and no date are set ("A date for the final changeover has not been set at this time", blog post of 21 July 2026).
- **Flags in v1.22.0** (`pkg/server/config/config.go`): `weighted_graph_check`, `shadow_weighted_graph_check`, `shadow_check`, `datastore_throttling`, `enable-check-optimizations`, and others. **[UNVERIFIED]** the exact behaviour of `shadow_weighted_graph_check`. By its name and the `shadow_check` pattern, it runs both engines and logs differences.
- **What it refuses when a model is built:**
  - recursion through `and` or `but not` (pure union recursion is fine);
  - `rel from parent` where an allowed type of `parent` lacks `rel`.

  It also errors at request time on a userset or wildcard subject that reaches a `but not`.
- **Depth:** the fixed limit of 25 is replaced by throttling, graph flattening and cancellation. Our `MAX_DEPTH = 25` stays right for the v1 engine we pin.
- **Error handling:** union branches tolerate errors; intersection and exclusion fail fast.

## 3. Custom roles scoped to an organization

- OpenFGA's own custom-roles guide models a role as its own type with an `assignee` relation. **It doesn't cover tenant isolation:** nothing scopes a role to the organization that owns it. The `org` relation on `role`, and the guard `assignee and member from org` (PR #99), are our addition.
- **What the model can close, and what it can't:**
  - **Case 1, closed by the model:** a foreign role assigned to this organization's members grants nothing.
  - **Case 2, can't be closed in the model:** a person who is a member of both organizations. OpenFGA can't compare two organizations, so the grant API (E3.F3.8.S2) and the reconciler (E3.F3.8.S3) close it.
  - The model also closes case 1 only while a role has exactly one `org` tuple. OpenFGA can't enforce "exactly one", so a role's `org` is written once, never added to, and a second one is drift (#72).

## 4. Limits, side by side

| Limit | OpenFGA v1.22.0 default | Inkcast constant |
|---|---|---|
| Resolution depth | 25 (`DefaultResolveNodeLimit`) | `MAX_DEPTH = 25` (`libs/authz/engine.py`) |
| Resolution breadth | 10 (`DefaultResolveNodeBreadthLimit`) | none (the local engine is sequential) |
| Checks per BatchCheck | 50 | `MAX_BATCH_CHECK = 50` |
| Tuples per Write | 100 | `MAX_TUPLES_PER_WRITE = 100` (`libs/authz/openfga.py`) |
| ListObjects results / deadline | 1000 / 3 s | `MAX_LIST_OBJECTS = 1000` |
| ListUsers results / deadline | 1000 / 3 s | not used yet |
| Types per model / model size | 100 / 256 KiB | well under |
| Condition evaluation cost | 100 | no conditions yet |

These are OpenFGA **server** settings. If a deployment changes them (`OPENFGA_MAX_CHECKS_PER_BATCH_CHECK` and the like), our constants must follow. That's tracked as part of the engine-parity work in #72.

## 5. Actions

1. **Keep the pin and the local engine in step.** When `FGA_VERSION` (CLI) or the OpenFGA server image is bumped, read the release notes for check-semantics changes, and run the yaml suite in both engines (already in CI).
2. **When the OpenFGA component ships (E3.F3.9):** run staging with `shadow_weighted_graph_check` (**[UNVERIFIED]** that it only logs) to collect differences before the new engine becomes the default.
3. **Model rules for new relations** (add to the contributor checklist with the next model change):
   - no recursion through `and` or `but not`;
   - every `rel from parent` must exist on every type `parent` allows;
   - never rely on a group being a member of itself.

## 6. `openfga migrate`: no lock, and what an interrupted run leaves

*Added 2026-11-04 for E3.F3.9.S8 (issue #337) and the review of PR #374. Read over git at the tags v1.22.0 and v1.22.1 (the files below are the same blobs at both), goose v3.28.0 (OpenFGA's `go.mod:35`), PostgreSQL `REL_18_STABLE`, Kubernetes v1.37.0.*

| Fact | Where |
|---|---|
| `openfga migrate` opens the database, pings it with a back-off bounded by `OPENFGA_TIMEOUT` (that is the flag's only use), reads the version with the package-level `goose.GetDBVersion` and runs the package-level `goose.Up`. **No lock**: goose's PostgreSQL session lock exists only on its `Provider` API, behind `WithSessionLocker`, which OpenFGA does not construct. | `pkg/storage/migrate/migrate.go:23-24,113-141`; `cmd/migrate/flags.go:29-30`; goose `up.go:161-168`, `provider_run.go:291-295`, `provider_options.go` |
| goose plans every file above the table's highest version once and applies them in order without re-reading the table between files. | goose `up.go:63-117` |
| goose's version table has no unique constraint on `version_id`; the current version is the **newest applied row by id**. | goose `internal/dialects/postgres.go:19-26,44-47`; `migrate.go:216-252` |
| A transactional file runs its statements and its version row in one transaction; a `-- +goose NO TRANSACTION` file runs each statement and then the version row on their own. | goose `migration_sql.go:27-89` |
| Migration 003 creates `idx_reverse_lookup_user`; 006 (`NO TRANSACTION`) creates `idx_user_lookup` with `CREATE INDEX CONCURRENTLY IF NOT EXISTS` and drops `idx_reverse_lookup_user`. So two runners that both planned 003-006 can let the slower one's 003 **succeed again** after the faster one's 006 dropped the index, leaving a newer row at version 3; its 004 then fails (the column exists) and nothing rolls the 003 row back. | `assets/migrations/postgres/003_add_reverse_lookup_index.sql:2`, `006_add_collate_index.sql:1-11`, `004_add_authorization_model_serialized_protobuf.sql:2` |
| That database reports revision 3 to goose and to OpenFGA, and v1.22 needs at least 4: `/healthz` stays NOT_SERVING and no retry repairs it. | `internal/build/build.go:16-18`; `pkg/storage/sqlcommon/sqlcommon.go:1244-1269` |
| An interrupted `CREATE INDEX CONCURRENTLY` leaves the index in the catalog marked INVALID; `IF NOT EXISTS` checks the name only, so a later 006 skips the build and records version 6 over an unusable index. Recovery is to drop it and build again. | PostgreSQL `doc/src/sgml/ref/create_index.sgml`, "Building Indexes Concurrently" |
| A session-level advisory lock is keyed by the database it is taken in (`MyDatabaseId`), blocks other takers of the same key until released, and is released by the server when the session ends, however it ends. `pg_locks` shows a bigint key split as `classid` (high half) and `objid` (low half) with `objsubid` 1. | `src/backend/utils/adt/lockfuncs.c` (`SET_LOCKTAG_INT64`), `src/include/storage/lock.h` (`SET_LOCKTAG_ADVISORY`), `doc/src/sgml/mvcc.sgml` "Advisory Locks" |
| psql exits 2 when the connection is lost in a non-interactive session, and 3 on an SQL error with `ON_ERROR_STOP`; `ON_ERROR_STOP` does not act on a failed `\!` shell command, which only sets `SHELL_ERROR` and `SHELL_EXIT_CODE` (measured 2026-11-04 on psql 18.6). | `doc/src/sgml/ref/psql-ref.sgml`, "Exit Status", `SHELL_ERROR` |
| The kubelet starts a later init container for the first time only after an earlier sidecar's startup probe succeeded, but restarts a **failed** init container from its own exited state, without waiting for that probe again: a lock held by a sidecar would not cover a retried migration. | `pkg/kubelet/kuberuntime/kuberuntime_container.go`, `computeInitContainerActions` (the `ContainerStateRunning` and `ContainerStateExited` cases) |
| `volumes[].image` (an image volume) is stable from Kubernetes 1.36 and on by default from 1.35, which OpenShift 4.22 runs (4.22.1: Kubernetes 1.35.5; the fork keeps the default), and is admitted by Pod Security "restricted". | kubernetes/website `feature-gates/ImageVolume.md`; openshift/kubernetes `release-4.22` `pkg/features/kube_features.go` (`ImageVolume`); pod-security-admission v0.37.0 `policy/check_restrictedVolumes.go` |
| `openfga migrate` parses its URI with Go's `net/url` before it applies `--datastore-password` (`OPENFGA_DATASTORE_PASSWORD`, "overwrite the password in the connection string") and prints a URI `net/url` refuses, password included (`Error: invalid database uri: parse "postgres://u:Pw\|...@...": net/url: invalid userinfo`, measured, issue #410); the override it re-encodes with `url.UserPassword`. The server parses with pgx's own parser, which takes what libpq takes (a raw `\|` in the password) and redacts the password in every error (`cannot parse `postgres://openfga:xxxxx@...``, measured), but with a username or password override set it parses the URI with `net/url` first and prints the same error, password included (measured). So the init container splits the URI for `openfga migrate` and the server gets the whole URI with no override. | `pkg/storage/migrate/migrate.go:80-94`, `cmd/migrate/migrate.go:42`, `pkg/storage/postgres/postgres.go:62-72`; pgx v5.11.0 (`go.mod:28`) `pgconn/parse_url.go:60-95,304-312`, `pgconn/errors.go` (`ParseConfigError.Error`, `redactURLPassword`) |

**What Inkcast does (E3.F3.9.S8):** `deploy/kustomize/components/openfga/migrate.sh`, run by the init container `migrate` with the PostgreSQL image and OpenFGA's own image as an image volume, holds one session-level advisory lock (key 1473304569, CRC-32 of `inkcast/openfga-migrate`, derived as goose derives its own 4097083626 and deliberately not that one) across the version discovery, the repair of an INVALID `idx_user_lookup` (dropped, and goose's version-6 rows removed so 006 runs again) and the whole `openfga migrate`; the Deployment is `strategy: Recreate` with one replica. The proofs are `scripts/dev/openfga-recovery-e2e.sh`.

## 7. The hand-over after a lock lost alone: a time grace, or a fence

*Added 2026-11-04 for issue #474 (K5, from the review of PR #453). Option 2, the eviction fence, is built (issue #494, 2026-11-05: "Built", below). Sources:*
- *read over git: PostgreSQL `REL_18_6`; pgx v5.11.0 (OpenFGA v1.22.0's `go.mod:28`); Go's `database/sql` at go1.26.0 (OpenFGA's `go.mod`: `go 1.26.0`); goose v3.28.0; CloudNativePG v1.30.1;*
- *read on the web: the PostgreSQL 18 manual and the Kubernetes documentation;*
- *measured: on 2026-11-04, in the migration harness's container (the pinned PostgreSQL 18.6 and OpenFGA v1.22.0 images), with a scratch script outside the repository.*

**The design today.** It is described in steps 2, 2b and 4 of `migrate.sh`'s header:
- One psql session takes the advisory lock. It probes itself every `PROBE_SECONDS` while `openfga migrate` runs in the background. A probe that finds the session gone kills the migration.
- So a runner whose lock session dies alone migrates on until its next probe.
- The runner that takes the lock next therefore waits a grace before it repairs and migrates: `2 × PROBE_SECONDS + 2` s (6 s by default), and only when another session of its role exists.

A grace is a time bound, not a fence: a runner that is paused rather than dead wakes up after the grace has passed. The review of PR #453 measured this residual: in 29 of 33 samples, the next runner held the lock while the paused runner's statement still ran (issue #474).

| Fact | Where |
|---|---|
| A paused client's statement runs on. With `openfga migrate` stopped (SIGSTOP) during 006, its `CREATE INDEX CONCURRENTLY` was still building 6 s later. `client_connection_check_interval` finds a client that has gone, not one that is paused with its connection open. | measured |
| A role may end its own sessions. `pg_terminate_backend` is allowed when the caller has the privileges of the backend's role, or of `pg_signal_backend`. It never ends a superuser's backend unless the caller is a superuser. With a timeout in milliseconds, it waits for the backend to exit; if the backend does not exit in time, it returns false with a warning. | `src/backend/storage/ipc/signalfuncs.c:50-127` (`pg_signal_backend`; the role checks at 88-103) and `:226-279` (`pg_terminate_backend` and its timeout); the PostgreSQL 18 manual, "Server Signaling Functions" |
| As the migrating role, `pg_terminate_backend(pid, 5000)` on the paused runner's session returned true in 0.15 s. The build ended and left `idx_user_lookup` INVALID. The thawed `openfga migrate` exited 1 (`failed to run SQL migration ... CREATE INDEX CONCURRENTLY`), and no version-6 row was recorded. On a superuser's backend, the same call failed with `permission denied to terminate process`. | measured |
| The migrating roles are not superusers. `openfga_staging` and `openfga_prod` are CloudNativePG managed roles with no `superuser` set, and it defaults to false. | `deploy/kustomize/shared/inkcast-db/cluster.yaml:32-41`; CloudNativePG v1.30.1 `api/v1/cluster_types.go:2721-2724` |
| `openfga migrate` names its sessions with `PGAPPNAME`. pgx maps it to `application_name`, and the URI's own settings override the environment's. OpenFGA sets no `application_name` of its own. Measured: with `PGAPPNAME=openfga-migrate-goose`, its session showed that name. Until #494, `migrate.sh` exported `PGAPPNAME=openfga-migrate` for both psql and `openfga migrate`, so their sessions could not be told apart; `openfga migrate` now runs with `PGAPPNAME=openfga-migrate-goose` on its command alone. | pgx `pgconn/config.go:701-739` (`parseEnvSettings`) and `:428` (`mergeSettings(defaultSettings, envSettings, connStringSettings)`); OpenFGA `pkg/storage/migrate/migrate.go` (no `application_name`); `migrate.sh` (`export PGAPPNAME`, and the `openfga migrate` command of step 4) |
| Ending a connection does not end the process. Before pgx's `database/sql` driver reuses a connection that has been idle for more than 1 s, it pings it, and it reports a failed ping as `driver.ErrBadConn`. `database/sql` then retries on another connection, and on its last try it opens a new one. So a runner thawed between two statements reconnects and goes on. **Read in source, not measured.** | pgx `stdlib/sql.go:566-598` (`ResetSession`) and `:148-151` (`OptionShouldPing`, default 1 s); Go `src/database/sql/sql.go:1572-1584` at go1.26.0 (`maxBadConnRetries`, `retry`) |
| goose's `Provider` takes its session lock on a dedicated `*sql.Conn` and runs the migrations on that same connection, so the lock and the migration cannot come apart. OpenFGA calls goose's package-level functions instead. OpenFGA's refactor toward a migration interface of its own is open. | goose v3.28.0 `provider_run.go:277-336` (`initialize`); openfga/openfga#2448 (open); #3355 (the INVALID index, open) |
| A fence needs a check by the storage, not by the lock holder: "the storage server to take an active role in checking tokens". | Kleppmann, "How to do distributed locking" (2016) |
| Kubernetes fences a node only when told to. With the `node.kubernetes.io/out-of-service` taint, "the pods on the node will be forcefully deleted", and "it should be verified that the node is already in shutdown or power off state". The feature has been stable since 1.28. | Kubernetes documentation, "Node Shutdowns", "Non-graceful node shutdown handling" |

### Options

1. **Keep the time grace (today).**
   - **It closes:** a runner whose lock session died while the runner itself runs at its pace, or at up to half its pace. Its own probe ends it within the grace.
   - **It fails** when a runner is paused for longer than the grace:
     - its statement in flight runs on during the pause (measured above), beside the next runner's work;
     - once thawed, it runs on for up to `PROBE_SECONDS`, until its own probe ends it.
   - **What covers that failure:** the repair (step 3) and a retry cover 006. They do not cover the one overlap that is permanent (§6): a runner paused after it planned 003 to 006, and thawed after the other runner's 006 dropped `idx_reverse_lookup_user`, records version 3 again. That needs a database below version 3 (a new install, or an upgrade from before 003) and a pause at that moment.
2. **Fence by eviction (built: issue #494).**
   - **How it works:**
     - `openfga migrate` gets a name of its own (`PGAPPNAME=openfga-migrate-goose`, on its command only).
     - Once the runner has the lock, before it repairs anything, it ends every session of its role and database that has that name, with `pg_terminate_backend(pid, 5000)`.
     - It fails the run when any of those sessions does not end within the timeout.
     - A URI's `application_name=` would override that name, so it is refused, as `options=` is.
   - **It closes:** the residual that was measured. A paused runner's statement in flight ends in about 0.15 s (measured), whatever the grace. Its failed statement then fails its whole `openfga migrate`.
   - **It fails** in these cases:
     - A runner thawed between two statements, or after the eviction, gets a new connection from `database/sql` (read in source, not measured). It migrates on until its own probe ends it, for up to `PROBE_SECONDS`. So the 003-after-006 overlap of option 1 stays possible.
     - A backend that does not exit in time fails the run, and the kubelet retries it. **[UNVERIFIED]** A backend whose client is cut off may wait on its socket while it ends.
   - **What it costs:** one statement, one environment variable and one more refused keyword. Nothing is added to OpenFGA's schema.
   - **The grace stays:** it lets a live runner end itself, and the eviction ends what a paused runner left running.
3. **A fence the database checks: a trigger on goose's version table.**
   - **How it works:**
     - Each run gives both its sessions, psql's and `openfga migrate`'s, a name with a token of its own (`application_name=openfga-migrate-<token>`).
     - A `BEFORE INSERT` trigger on `goose_db_version`, which the role owns, raises an error unless the session that holds the lock has the same name as the inserting session.
   - **It closes:** every transactional migration of a runner that lost the lock. Its version row is refused, so its DDL rolls back with it. That includes the late 003, the overlap that is permanent. For 006 (`NO TRANSACTION`), only the version row is refused: the build has already run, and the repair covers it.
   - **It fails or costs:**
     - It puts an object in OpenFGA's schema that OpenFGA does not know about. A later OpenFGA or goose that changes the version table, or writes to it outside `openfga migrate`, will meet it.
     - It adds PL/pgSQL to a shell script, and the trigger needs a repair of its own if it is ever dropped.
     - **[UNVERIFIED]** It is neither built nor measured.
4. **Upstream: OpenFGA locks its own migration.**
   - goose's `Provider` with a session locker holds the lock on the connection that migrates (see the table). For a binary used as a black box, that is the only true fence.
   - The script's lock and grace would stay as a second guard until every supported OpenFGA version has it.
   - Nobody has asked for it yet. The place to ask is openfga/openfga#2448.
5. **Fence the node, not the session.**
   - `strategy: Recreate` ends the old pod before the new one starts. So a runner still alive after that belongs to a pod whose node is partitioned, or whose object was force-deleted.
   - Fencing that is the platform's job: the out-of-service taint, applied once the node is verified to be off. It is outside the init container's reach.
   - It is no substitute for 1 to 4 in the script, which cannot see the node.

### Recommendation

- **Build option 2, the eviction fence, beside the grace, as a story of its own:**
  - It closes the residual that was measured: 29 of 33 samples, and a build still running 6 s into a pause.
  - It uses the role's own privilege and adds nothing to OpenFGA's schema.
  - What it leaves is short and covered: up to `PROBE_SECONDS` after a thaw, before the runner's own probe ends it. The repair and the retry cover 006.
  - Its tests: the paused-runner case of the review of PR #453, with the eviction measured, and a refused `application_name=`.
- **Raise option 4 upstream, on openfga/openfga#2448.** It is the only fence that closes the late-003 overlap without an object in OpenFGA's schema.
- **Do not build option 3 unless a late 003 is ever seen.** It fences a rare, permanent case at the price of a foreign object in OpenFGA's schema.

The integrator decided on 2026-11-04 to build option 2 beside the grace, which stays, and not to build option 3 unless a late 003 is ever seen (issue #494).

### Built (issue #494)

*Built on 2026-11-05. Measured in the migration harness (the pinned PostgreSQL 18.6 and OpenFGA v1.22.0 images).*

- **What `migrate.sh` does** (header, steps 2b and 4):
  - `openfga migrate` runs with `PGAPPNAME=openfga-migrate-goose` on its command alone. psql's lock session keeps `openfga-migrate`.
  - After the grace, before the repair, the runner that holds the lock runs `pg_terminate_backend(pid, 5000)` on every session with `usename = current_user`, `datname = current_database()` and that name, except its own backend.
  - `pg_terminate_backend` returns false both on a timeout and for a pid that is no backend any more (`signalfuncs.c:67-76`, `:217-223`). So the runner looks again in a statement of its own, because `pg_stat_activity` is read once per transaction (`monitoring.sgml`). A pid still there fails the run, and the log line names it. A pid that ended on its own is not a failure.
  - It logs pids and counts only. When there is nothing to end, it logs nothing, so a run without the race is unchanged.
  - A URI whose query sets `application_name=` (any case or encoding of the key, `fallback_application_name=` included) is refused before psql, as `options=` is.
  - A URI with `service=` or an inherited `PGSERVICE` is refused before psql. pgx v5.11.0 reads service file settings after `PGAPPNAME`. An offline `pgconn.ParseConfig` check changed `application_name` from `openfga-migrate-goose` to `out-of-fence` when the URI selected a service file.
  - If the fence query has no answer after 60 s, the runner fails and names the fence. More than 12 backends that each take the full 5 s can cause this.
- **Measured:**
  - With `PGAPPNAME=openfga-migrate-goose`, the session building 006 showed `openfga-migrate-goose|openfga|openfga` (name, `usename`, `datname`) and the lock holder `openfga-migrate`.
  - With `application_name=x` in the URI as well, the build ran as `x`, and the fence's statement left it running.
  - A runner was paused (SIGSTOP on all its processes) during 006, and its lock session was ended alone. The next runner waited the grace (6 s) and then ended the paused build, 6.1 s after it started, with the snapshot that build waited for still held. Its repair dropped the INVALID index and its 006 built it again.
  - Thawed, the paused runner's `openfga migrate` failed (`FATAL: terminating connection due to administrator command (SQLSTATE 57P01)`) and the runner exited 1. One version-6 row remained, over a valid `idx_user_lookup`.
  - Before the fence, the next runner's repair (`DROP INDEX CONCURRENTLY`) waited on that build until its session gave up after 60 s.
- **The tests:** `tests/test_migrate_eviction_fence.py`.
- **Still open, as option 2 above says:**
  - A runner thawed between two statements, or after the fence, gets a new connection from `database/sql` (read in source, not measured). It runs on until its own probe ends it.
  - **[UNVERIFIED]** A backend whose client is cut off may not exit within the 5 s, and that fails the run.

## Sources

- [OpenFGA's Move to Weighted Graph Resolution: What's Changing](https://openfga.dev/blog/weighted-graph-upcoming-changes), 21 July 2026.
- [`v2breaking` package (Go docs)](https://pkg.go.dev/github.com/openfga/openfga/pkg/server/commands/v2breaking): the request shapes whose v1 and v2 answers differ.
- [Configuring OpenFGA](https://openfga.dev/docs/getting-started/setup-openfga/configure-openfga): experimental flags.
- [Modeling custom roles](https://openfga.dev/docs/modeling/custom-roles).
- [Usersets](https://openfga.dev/docs/modeling/building-blocks/usersets).
- OpenFGA v1.22.0 source (`pkg/server/config/config.go`) and openfga/cli v0.8.2 `go.mod`, read over git on 2026-10-22.
- **[CONFLICT]** The GitHub releases page fetched on the same day listed v1.19.0 as the latest release. The CLI's `go.mod` and the v1.22.0 tag (6 October 2026) show it was stale, so the version facts here come from the tags.
- [Server Signaling Functions](https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-ADMIN-SIGNAL), PostgreSQL 18 manual: `pg_terminate_backend` and its `timeout` (§7).
- [How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html), Martin Kleppmann, 2016: fencing tokens (§7).
- [Node Shutdowns](https://kubernetes.io/docs/concepts/cluster-administration/node-shutdown/), Kubernetes documentation: "Non-graceful node shutdown handling" (§7).
- [openfga/openfga#2448](https://github.com/openfga/openfga/issues/2448), "Migrate: Refactor for migration improvements", and [#3355](https://github.com/openfga/openfga/issues/3355), "Failed migration left invalid index", both open on 2026-11-04 (§7).
- PostgreSQL `REL_18_6` (`signalfuncs.c`), pgx v5.11.0 (`pgconn/config.go`, `stdlib/sql.go`), Go go1.26.0 (`database/sql/sql.go`), goose v3.28.0 (`provider_run.go`) and CloudNativePG v1.30.1 (`api/v1/cluster_types.go`), read over git on 2026-11-04 (§7).

---
<!-- inkcast-nav -->
[Docs](../../docs/README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
