#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# OpenFGA's schema migration under a database lock (E3.F3.9.S8, issue #337; the review of PR #374). The init
# container `migrate` of OpenFGA's pod (deployment.yaml) runs this with the PostgreSQL image, for psql, and OpenFGA's
# own image mounted read-only as an image volume, for its binary. OpenFGA v1.22 takes no lock around its migrations
# (pkg/storage/migrate/migrate.go:132-141 at v1.22.0 and v1.22.1: the package-level goose.GetDBVersion and goose.Up,
# goose v3.28.0 up.go:161-168, without the Provider's session locker), so two runners can overlap and, through
# 003's index that 006 drops, leave goose reporting revision 3 for good; and 006, `-- +goose NO TRANSACTION`, can
# leave `idx_user_lookup` INVALID when its concurrent build is interrupted, which a later run's IF NOT EXISTS skips.
# One psql session, driven by this shell as a coprocess, does the whole run, so one lock covers it all:
#
#   1. wait for the database, CONNECT_TIMEOUT seconds at most (a fresh Cluster bootstraps; the shared one waits for
#      this environment's role): then exit 1, and the kubelet starts this container again;
#   2. take the session-level advisory lock LOCK_KEY on OpenFGA's database: pg_try_advisory_lock every few seconds,
#      idle in between, as goose's own locker does (lock/postgres.go:109-125 at v3.28.0), naming the holder while
#      it waits. Never the blocking pg_advisory_lock: a session blocked in it is a statement in progress, holding a
#      snapshot, and the holder's 006 (CREATE INDEX CONCURRENTLY waits for every older snapshot) would wait on the
#      waiter while the waiter waits on the holder, a deadlock PostgreSQL cannot see (measured on kind, 2026-11-04).
#      The server releases the lock when this session ends, however it ends (mvcc.sgml "Advisory Locks" at
#      REL_18_STABLE; keyed by database: lockfuncs.c SET_LOCKTAG_INT64 uses MyDatabaseId, so staging and production
#      on one PostgreSQL never wait on each other);
#   3. repair what an interrupted 006 leaves: `idx_user_lookup` INVALID is dropped, and goose's version-6 rows go
#      when that index is invalid or missing, so that `openfga migrate` applies 006 again (goose up.go:99-106 at
#      v3.28.0 plans every file above the table's highest version). Not once a version above 6 is recorded: goose
#      then refuses a 6 missing below it on every run (up.go:77-90). Nothing else is touched: no tuple, no model;
#   4. `openfga migrate`, its version discovery and every migration, while the session holds the lock, idle;
#   5. release the lock. A session that died meanwhile (the database restarted) fails this run, and the kubelet runs
#      it again from step 1. What `openfga migrate` did between the loss and the retry ran without the lock: the
#      retry's repair covers 006, and a retry finds the revision current otherwise.
#
# This script shows no credential: psql connects with the URI less its password and reads the password from
# PGPASSWORD, because a process's argv is readable by every user of the node (/proc/<pid>/cmdline, mode 0444; `ps`)
# while its environment is its own (environ, 0400), and because libpq quotes a URI it cannot parse in the errors the
# wait for the database logs; otherwise they name the host, the port and the user. The URI itself is never echoed.
# OpenFGA gets the same URI less its password, with the password in OPENFGA_DATASTORE_PASSWORD (step 4): it parses
# the URI with Go's net/url and prints one it refuses, password included (a raw `|` in the password, which libpq
# accepts: "invalid database uri: parse ...: net/url: invalid userinfo", measured with v1.22.0; issue #410).
# The lab's e2e proofs are scripts/dev/openfga-recovery-e2e.sh.
set -euo pipefail
: "${OPENFGA_DATASTORE_URI:?OPENFGA_DATASTORE_URI is not set: the Secret openfga-db-app, key uri}"
OPENFGA_BINARY="${OPENFGA_BINARY:-/opt/openfga/openfga}"
connect_timeout="${CONNECT_TIMEOUT:-600}"
# CRC-32 of "inkcast/openfga-migrate", derived as goose derives its own default (4097083626, CRC-32 of "goose",
# lock/session_locker_options.go at v3.28.0) and deliberately not that one: an OpenFGA that one day locks there
# itself, inside the run this session wraps, would wait on this session for ever.
lock_key="${LOCK_KEY:-1473304569}"
export PGAPPNAME=openfga-migrate PGCONNECT_TIMEOUT=10
# Server-side keepalives on these sessions: a runner cut off without a FIN (a node partition) holds the lock until the
# server finds it gone, after the OS's 7200 s of idle with tcp_keepalives_idle 0, PostgreSQL's and CloudNativePG's
# default; with these, in about 35 s. And a client check while a statement runs: a killed pod's lock goes with psql's
# session at once, but OpenFGA's backend would run its statement on (006's CREATE INDEX CONCURRENTLY: as long as the
# table takes) without the lock. OpenFGA's pgx reads PGOPTIONS too (pgconn/config.go at v5.11.0). The proofs are
# tests/test_review_openfga_migrate.py.
export PGOPTIONS="-c tcp_keepalives_idle=20 -c tcp_keepalives_interval=5 -c tcp_keepalives_count=3 -c client_connection_check_interval=2s"
# psql's URI, less the password, which goes to PGPASSWORD percent-decoded as libpq decodes it (the header says why).
db_uri="$OPENFGA_DATASTORE_URI"
if [[ "$db_uri" =~ ^([^:/?#]+://[^:@/?#]*):([^@/?#]*)@(.*)$ ]]; then
  encoded="${BASH_REMATCH[2]//%/\\x}"
  printf -v PGPASSWORD '%b' "$encoded"
  export PGPASSWORD
  db_uri="${BASH_REMATCH[1]}@${BASH_REMATCH[3]}"
  unset encoded
fi
log() { printf '%s migrate: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

[ -x "$OPENFGA_BINARY" ] || { log "no OpenFGA binary at $OPENFGA_BINARY: is the image volume mounted?"; exit 1; }

# 1. The database, with its role and its database created: a connection as the role, not a bare ping.
deadline=$(( $(date +%s) + connect_timeout ))
attempt=0
while ! out="$(psql -X -qAt -v ON_ERROR_STOP=1 -c 'SELECT 1' "$db_uri" 2>&1)"; do
  attempt=$(( attempt + 1 ))
  if [ "$(date +%s)" -ge "$deadline" ]; then
    log "no database after ${connect_timeout} s (${attempt} attempts): ${out%%$'\n'*}"
    exit 1
  fi
  [ $(( attempt % 6 )) -eq 1 ] && log "waiting for the database (attempt ${attempt}): ${out%%$'\n'*}"
  sleep 5
done
log "database reachable (${attempt} failed attempts); taking the migration lock ${lock_key}"

# 2 to 5 in one session: psql as a coprocess, one statement at a time (terminated with `;`, since psql keeps an
# unterminated statement in its buffer while a backslash command runs at once), each answer read back up to a
# sentinel into `reply`. `query` runs in this shell, never in a subshell (a `$(...)` or a pipeline cannot reach the
# coprocess, and an exit inside one would not end the run), so a dead session really ends the run: ON_ERROR_STOP
# makes an SQL error end psql (exit 3) and psql exits on its own (2) when the connection is lost. Results are plain
# lines (tuples only, unaligned).
coproc PSQL { psql -X -q -At -v ON_ERROR_STOP=1 "$db_uri"; }
# shellcheck disable=SC2153  # PSQL_PID: bash sets NAME_PID for a coprocess named NAME
psql_pid=$PSQL_PID
reply=""
query() {  # query SQL: the statement's result lines in `reply`, or a failed run when the session is gone or silent for 60 s
  local line
  reply=""
  { printf '%s;\n\\echo __done__\n' "$1" >&"${PSQL[1]}"; } 2>/dev/null || { log "the database session is gone"; exit 1; }
  while IFS= read -r -t 60 line <&"${PSQL[0]}"; do
    [ "$line" = "__done__" ] && { reply="${reply%$'\n'}"; return 0; }
    reply+="${line}"$'\n'
  done
  if kill -0 "$psql_pid" 2>/dev/null; then
    log "no answer from the database session in 60 s"
  else
    log "the database session ended (the database restarted, or a statement failed above)"
  fi
  exit 1
}

# 2. The lock.
waited=0
query "SELECT pg_try_advisory_lock(${lock_key})"
until [ "$reply" = t ]; do
  if [ $(( waited % 30 )) -eq 0 ]; then
    query "SELECT coalesce(string_agg(format('pid %s (%s, %s, since %s)', a.pid, a.application_name, a.client_addr, a.backend_start), ', '), 'nobody')
      FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
      WHERE l.locktype = 'advisory' AND l.objsubid = 1 AND l.granted AND ((l.classid::bigint << 32) | l.objid::bigint) = ${lock_key}"
    log "waiting for the migration lock, held by ${reply}"
  fi
  sleep 3; waited=$(( waited + 3 ))
  query "SELECT pg_try_advisory_lock(${lock_key})"
done
query "SELECT format('migration lock taken by pid %s at %s', pg_backend_pid(), clock_timestamp())"
log "$reply"

# 3. The repair, under the lock. to_regclass honours the search path, and is NULL when the object is missing.
query "SELECT (to_regclass('idx_user_lookup') IS NOT NULL
               AND NOT coalesce((SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_user_lookup')), true))::text,
              (to_regclass('idx_user_lookup') IS NULL)::text,
              (to_regclass('goose_db_version') IS NOT NULL)::text"
IFS='|' read -r idx_invalid idx_missing versioned <<< "$reply"
if [ "$versioned" = true ] && { [ "$idx_invalid" = true ] || [ "$idx_missing" = true ]; }; then
  query "SELECT EXISTS (SELECT 1 FROM goose_db_version WHERE version_id > 6)"
  if [ "$reply" = t ]; then
    log "repair: skipped: idx_user_lookup is invalid or missing, but goose records a version above 6, so 006 cannot run again"
    idx_invalid=false idx_missing=false
  fi
fi
if [ "$idx_invalid" = true ]; then
  log "repair: idx_user_lookup is INVALID (an interrupted 006), dropping it so that 006 builds it again"
  query "DROP INDEX CONCURRENTLY idx_user_lookup"
fi
if [ "$versioned" = true ] && { [ "$idx_invalid" = true ] || [ "$idx_missing" = true ]; }; then
  query "SELECT EXISTS (SELECT 1 FROM goose_db_version WHERE version_id = 6)"
  if [ "$reply" = t ]; then
    log "repair: version 6 is recorded without a valid idx_user_lookup, removing its rows so that 006 is applied again"
    query "DELETE FROM goose_db_version WHERE version_id = 6"
  fi
fi

# 4. The migration, while the session idles with the lock. A statement first, so a session already lost never
# lets OpenFGA start; OpenFGA's own exit code is the run's. OpenFGA gets psql's URI, less the password, and the
# password on its own: `--datastore-password`, OPENFGA_DATASTORE_PASSWORD, "overwrite the password in the connection
# string" (cmd/migrate/migrate.go:42 at v1.22.0). It parses the URI with Go's net/url before that override
# (pkg/storage/migrate/migrate.go:80-94) and prints a URI it refuses, password included (#410); the override it
# encodes itself (url.UserPassword), so the password reaches pgx whatever it holds. Empty when the URI had none.
query "SELECT format('lock held by pid %s, running openfga migrate', pg_backend_pid())"
log "$reply"
if OPENFGA_DATASTORE_URI="$db_uri" OPENFGA_DATASTORE_PASSWORD="${PGPASSWORD-}" "$OPENFGA_BINARY" migrate; then :; else
  code=$?
  log "openfga migrate exited ${code}: this run fails, the kubelet starts it again"
  exit "$code"
fi

# 5. The release: the session must still be the one that took the lock.
query "SELECT pg_advisory_unlock(${lock_key})"
if [ "$reply" != t ]; then
  log "the migration lock was not released by this session (the lock was not held): this run fails, the kubelet starts it again"
  exit 1
fi
query "SELECT format('migration lock released by pid %s at %s', pg_backend_pid(), clock_timestamp())"
log "$reply"
printf '\\q\n' >&"${PSQL[1]}"
wait "$psql_pid"
log "done"
