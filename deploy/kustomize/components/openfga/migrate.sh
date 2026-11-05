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
#   2b. a hand-over grace (#409): a runner whose lock session died alone still runs its migration until its next
#      probe (step 4), so when other sessions of this role exist, this runner waits twice the probe interval plus
#      the server's client check before touching the schema, as leader election hands a lease over (client-go's
#      LeaseDuration: a candidate waits it out before it acts). A grace is not a fence: a runner paused rather than
#      dead wakes after it, and its statement in flight runs on meanwhile, while `openfga migrate` runs goose as a
#      black box, with no token the database could check (Kleppmann, "How to do distributed locking"), where goose's
#      own Provider, golang-migrate and Flyway lock on the very connection that migrates. So an eviction fence follows
#      the grace (#494; docs/research/openfga-2026-10.md §7): `openfga migrate` runs under a name of its own,
#      openfga-migrate-goose (step 4), and this runner ends every session of its role and database with that name,
#      with pg_terminate_backend(pid, 5000) (a role may end its own sessions: signalfuncs.c:88-103 at REL_18_6), and
#      fails the run when one does not end in time. It closes a paused runner's statement in flight: that statement
#      ends, and fails its `openfga migrate`. It leaves open a runner thawed between two statements, or after the
#      fence: database/sql opens it a new connection, and it runs on until its own probe ends it, PROBE_SECONDS at
#      most; the repair of step 3 and a retry cover what that leaves to 006;
#   3. repair what an interrupted 006 leaves: `idx_user_lookup` INVALID is dropped, and goose's version-6 rows go
#      when that index is invalid or missing, so that `openfga migrate` applies 006 again (goose up.go:99-106 at
#      v3.28.0 plans every file above the table's highest version). Not once a version above 6 is recorded: goose
#      then refuses a 6 missing below it on every run (up.go:77-90). Nothing else is touched: no tuple, no model;
#   4. `openfga migrate`, its version discovery and every migration, in the background while the session holds the
#      lock and this shell probes the session every PROBE_SECONDS (#409): the session can die alone while this
#      container lives (pg_terminate_backend; a middlebox dropping an idle connection), and the lock goes with it,
#      so a probe that finds it gone ends this run and kills `openfga migrate`; the statement its backend had in
#      flight ends with its client (client_connection_check_interval, below), within seconds. A probe unanswered for
#      60 s ends the run the same way, so a database silent that long during 006 interrupts a build the run would
#      have finished: that fails safe, as the retry's repair drops the INVALID index and 006 runs again (#474);
#   5. release the lock. A session that died meanwhile (the database restarted) fails this run, and the kubelet runs
#      it again from step 1. What `openfga migrate` did between the loss and the next probe ran without the lock,
#      for seconds at most: the retry's repair covers 006, and a retry finds the revision current otherwise.
#
# This script shows no credential: psql connects with the URI less its password and reads the password from
# PGPASSWORD, because a process's argv is readable by every user of the node (/proc/<pid>/cmdline, mode 0444; `ps`)
# while its environment is its own (environ, 0400), and because libpq quotes a URI it cannot parse in the errors the
# wait for the database logs; otherwise they name the host, the port and the user. The URI itself is never echoed.
# OpenFGA gets the same URI less its password, with the password in OPENFGA_DATASTORE_PASSWORD (step 4): it parses
# the URI with Go's net/url and prints one it refuses, password included (a raw `|` in the password, which libpq
# accepts: "invalid database uri: parse ...: net/url: invalid userinfo", measured with v1.22.0; issue #410).
# A URI no split makes safe is refused before psql runs, in words that quote none of it (#474): a raw `@` or `/` in
# the password, which libpq and pgx misread (net/url splits at the last `@` and refuses a `/`); a password in the
# query, which would stay on psql's command line; options=, which would replace PGOPTIONS (below) and with it the
# client check that ends a killed runner's statement; and application_name= or service=, which could rename
# `openfga migrate`'s sessions out of the fence's reach (step 2b).
# The lab's e2e proofs are scripts/dev/openfga-recovery-e2e.sh.
set -euo pipefail
: "${OPENFGA_DATASTORE_URI:?OPENFGA_DATASTORE_URI is not set: the Secret openfga-db-app, key uri}"
OPENFGA_BINARY="${OPENFGA_BINARY:-/opt/openfga/openfga}"
connect_timeout="${CONNECT_TIMEOUT:-600}"
probe_seconds="${PROBE_SECONDS:-2}"
# CRC-32 of "inkcast/openfga-migrate", derived as goose derives its own default (4097083626, CRC-32 of "goose",
# lock/session_locker_options.go at v3.28.0) and deliberately not that one: an OpenFGA that one day locks there
# itself, inside the run this session wraps, would wait on this session for ever.
lock_key="${LOCK_KEY:-1473304569}"
# psql's sessions' name; `openfga migrate` runs under one of its own (step 4), which the fence of step 2b ends.
export PGAPPNAME=openfga-migrate PGCONNECT_TIMEOUT=10
# Server-side keepalives on these sessions: a runner cut off without a FIN (a node partition) holds the lock until the
# server finds it gone, after the OS's 7200 s of idle with tcp_keepalives_idle 0, PostgreSQL's and CloudNativePG's
# default; with these, in about 35 s. And a client check while a statement runs: a killed pod's lock goes with psql's
# session at once, but OpenFGA's backend would run its statement on (006's CREATE INDEX CONCURRENTLY: as long as the
# table takes) without the lock. OpenFGA's pgx reads PGOPTIONS too (pgconn/config.go at v5.11.0). The proofs are
# tests/test_review_openfga_migrate.py.
export PGOPTIONS="-c tcp_keepalives_idle=20 -c tcp_keepalives_interval=5 -c tcp_keepalives_count=3 -c client_connection_check_interval=2s"
log() { printf '%s migrate: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
# A service file can override PGAPPNAME in pgx. Do not let an inherited service select one for either connection.
if [ -n "${PGSERVICE-}" ]; then
  log "PGSERVICE is refused: a service file can override PGAPPNAME and hide openfga migrate's sessions from the fence"
  exit 1
fi
# $1 percent-decoded into `decoded` as libpq decodes (conninfo_uri_decode at REL_18_6, as pgx's uriDecode at v5.11.0):
# its leading and trailing spaces dropped, then each %XX, and nothing else; what libpq refuses there (a space left
# inside, a % without two hex digits after it, %00) refused in words that quote none of it. Its backslashes are
# doubled first, or printf's %b would read a `\n` or a `\c` in a password as an escape (#474).
uri_decode() {
  local value="${1#"${1%%[! ]*}"}" invalid='%([^0-9A-Fa-f]|.[^0-9A-Fa-f]|.?$)|%00'
  value="${value%"${value##*[! ]}"}"
  [[ "$value" != *' '* && ! "$value" =~ $invalid ]] ||
    { log "the datastore URI (the Secret openfga-db-app, key uri) is refused: its password or a key of its query holds a space inside it, a % without two hex digits after it, or %00, which libpq and pgx refuse (a space must be percent-encoded: %20, a % as %25)"; exit 1; }
  value="${value//\\/\\\\}"
  printf -v decoded '%b' "${value//%/\\x}"
}
# psql's URI, less the password, which goes to PGPASSWORD percent-decoded as libpq decodes it (the header says why).
# Split where libpq and pgx split: the userinfo ends at the first `@` before any `/`, so a raw `?` or `#` is part of
# the password (fe-connect.c conninfo_uri_parse_options at REL_18_6; pgconn/parse_url.go at v5.11.0).
db_uri="$OPENFGA_DATASTORE_URI"
if [[ "$db_uri" =~ ^([^:/?#]+://[^:@/]*):([^@/]*)@(.*)$ ]]; then
  db_uri="${BASH_REMATCH[1]}@${BASH_REMATCH[3]}"
  uri_decode "${BASH_REMATCH[2]}"
  export PGPASSWORD="$decoded"
  unset decoded
fi

# A URI no split makes safe, refused in words that quote none of it (#474): a string that is no URI (keyword=value),
# which psql takes whole on its command line and `openfga migrate` cannot read, whatever `://` it holds, since libpq
# and pgx read a URI only after postgresql:// or postgres:// (fe-connect.c uri_prefix_length at REL_18_6; pgconn/
# parse_url.go at v5.11.0), and anything else with an `=` as keyword=value; a raw `@` or `/` in the password,
# which libpq reads into the host or the port and quotes in the errors the wait for the database logs (net/url
# refuses a `/`); a raw `?` or `#` in the user name, where libpq reads on to the `@` and net/url, `openfga migrate`'s,
# ends the authority, and which started the query below early; a password in the query, which libpq and pgx honour
# over the user information's, its keyword percent-decoded (conninfo_uri_parse_params), and which the split leaves on
# psql's command line; options=, which replaces PGOPTIONS above whole (pgx: pgconn/config.go mergeSettings at
# v5.11.0), its client check included; application_name=, which renames `openfga migrate`'s sessions as pgx takes the
# URI's settings over PGAPPNAME's (mergeSettings, the same), so the fence would miss them (measured: a paused build
# under `x` ran on); service=, which can load a service file whose application_name overrides PGAPPNAME; and a
# control character outside the password, which net/url refuses (and
# prints the URI) and at which `read` below stops (a newline), so that a parameter after it would go unread.
# No writer of the Secret sets one: Inkcast's generator writes sslmode=require alone, CloudNativePG no query
# (pkg/specs/secrets.go at v1.30.1).
[[ "$OPENFGA_DATASTORE_URI" =~ ^postgres(ql)?://([^:@/?#]*(:[^@/]*)?@)?[^@]*$ ]] ||
  { log "the datastore URI (the Secret openfga-db-app, key uri) is refused: it is no URI, its user name holds a ? or a #, or an @ in it does not end its user information (an @ or a / in the password must be percent-encoded: %40, %2F)"; exit 1; }
[[ "$db_uri" != *[[:cntrl:]]* ]] ||
  { log "the datastore URI (the Secret openfga-db-app, key uri) is refused: it holds a control character (a newline?) outside its password, which OpenFGA's net/url refuses"; exit 1; }
if [[ "$db_uri" == *\?* ]]; then
  IFS='&' read -ra params <<< "${db_uri#*\?}"
  for param in "${params[@]}"; do
    uri_decode "${param%%=*}"
    case "${decoded,,}" in          # libpq refuses an upper-case key; the value would still be on psql's command line
      *password*|*secret*|scram_*) log "the datastore URI (the Secret openfga-db-app, key uri) is refused: it carries a password or a secret in its query (password=, sslpassword=, oauth_client_secret=, scram_client_key=), which psql's command line would show; the password goes in the user information"; exit 1 ;;
      *options*) log "the datastore URI (the Secret openfga-db-app, key uri) is refused: its query sets options=, which would replace PGOPTIONS and the client check that ends a killed runner's statement"; exit 1 ;;
      *application_name*) log "the datastore URI (the Secret openfga-db-app, key uri) is refused: its query sets application_name=, which would rename openfga migrate's sessions and hide them from the fence that ends a paused runner's statement"; exit 1 ;;
      *service*) log "the datastore URI (the Secret openfga-db-app, key uri) is refused: its query sets service=, which can override PGAPPNAME through a service file and hide openfga migrate's sessions from the fence"; exit 1 ;;
    esac
  done
fi

# The probe interval, whole seconds: sleep and the grace's arithmetic need one, a 0 would probe in a busy loop, and at
# half pace (2 x 4 + 2) a lock session that dies alone still ends its migration within the 10 s of #409.
[[ "$probe_seconds" =~ ^[1-4]$ ]] || { log "PROBE_SECONDS must be a whole number of seconds from 1 to 4, not ${probe_seconds}"; exit 1; }
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
query() {  # query SQL [timeout message]: the statement's result lines in `reply`, or a failed run after 60 s of silence
  local line status=0 timeout_message="${2:-no answer from the database session in 60 s}"
  reply=""
  # ${PSQL[n]-}: bash unsets PSQL once it reaps psql, and under set -u the bare expansion would end the run unlogged
  { printf '%s;\n\\echo __done__\n' "$1" >&"${PSQL[1]-}"; } 2>/dev/null || { log "the database session is gone"; exit 1; }
  while :; do
    IFS= read -r -t 60 line 2>/dev/null <&"${PSQL[0]-}" || { status=$?; break; }
    [ "$line" = "__done__" ] && { reply="${reply%$'\n'}"; return 0; }
    reply+="${line}"$'\n'
  done
  # read's own status tells a timeout (above 128) from the end of psql's output; `kill -0` can't, as it succeeds on
  # a psql that has exited and that bash has not reaped yet (#510)
  if [ "$status" -gt 128 ]; then
    log "$timeout_message"
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

# 2b. The hand-over grace: twice the probe interval (a runner slowed to half its pace still probes within it) plus
# client_connection_check_interval (2 s, PGOPTIONS above: its backend's last statement ends with it). Only when
# another session of this role exists: a runner whose lock died, a waiter, or a server of a pod on its way out.
query "SELECT count(*) FROM pg_stat_activity WHERE usename = current_user AND pid <> pg_backend_pid()"
if [ "$reply" != 0 ]; then
  grace=$(( 2 * probe_seconds + 2 ))
  log "${reply} other session(s) of this role: waiting ${grace} s for a runner whose lock session died to end its migration"
  sleep "$grace"
fi

# 2b, the fence (#494): a session of this role and database named openfga-migrate-goose is an earlier runner's
# `openfga migrate`, as this run's has not started; it ends here, before the repair. pg_terminate_backend waits up to
# 5 s for each backend to exit, and returns false, with a warning, when one has not, or when the pid was no backend
# any more: one that ended on its own meanwhile (signalfuncs.c:67-76 and 217-223 at REL_18_6). A second look tells
# them apart, in a statement of its own, as pg_stat_activity is read once a transaction (monitoring.sgml). Only
# pids and counts are logged.
query "SELECT count(*), coalesce(string_agg(pid::text, ', '), ''), coalesce(string_agg(pid::text, ',') FILTER (WHERE NOT ended), '')
       FROM (SELECT pid, pg_terminate_backend(pid, 5000) AS ended FROM pg_stat_activity
             WHERE usename = current_user AND datname = current_database() AND application_name = 'openfga-migrate-goose'
               AND pid <> pg_backend_pid()) AS fenced" "fence: no answer from the database session in 60 s while ending earlier runner sessions; this run fails, the kubelet starts it again"
IFS='|' read -r fenced fenced_pids unended <<< "$reply"
if [ -n "$unended" ]; then
  query "SELECT coalesce(string_agg(pid::text, ', '), '') FROM pg_stat_activity WHERE pid IN (${unended})" "fence: no answer from the database session in 60 s while checking which earlier runner sessions remain; this run fails, the kubelet starts it again"
  if [ -n "$reply" ]; then
    log "fence: openfga migrate's session of an earlier runner, pid ${reply}, did not end within 5 s: this run fails, the kubelet starts it again"
    exit 1
  fi
fi
[ "$fenced" = 0 ] || log "fence: ended ${fenced} session(s) of openfga migrate left by an earlier runner (pid ${fenced_pids})"

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

# 4. The migration, while the session holds the lock. A statement first, so a session already lost never lets
# OpenFGA start; OpenFGA's own exit code is the run's. `openfga migrate` runs in the background and this shell
# probes the session between sleeps (#409): `query` ends the run when the session is gone, through the EXIT trap,
# which kills the migration (SIGTERM ends a Go process at once, and `openfga migrate` handles no signal: its backend
# loses its client, and the server ends the statement at its next client check) and reaps it, as it does when the
# kubelet ends this container: a child terminated by a signal and left to whatever is PID 1 is a crash to a
# postmaster (measured: "untracked child process ... terminated by signal 15", then recovery). The probe holds no
# snapshot once it has answered, so 006's concurrent build never waits on this session. The trap goes once the
# migration has ended on its own. Each interval ends early when the migration does: `wait -n` returns with whichever
# of the two ends first, and bash keeps the migration's status for the `wait` below. OpenFGA gets psql's URI, less
# the password, and the password on its own: `--datastore-password`, OPENFGA_DATASTORE_PASSWORD, "overwrite the
# password in the connection string" (cmd/migrate/migrate.go:42 at v1.22.0). It parses the URI with Go's net/url
# before that override (pkg/storage/migrate/migrate.go:80-94) and prints a URI it refuses, password included (#410);
# the override it encodes itself (url.UserPassword), so the password reaches pgx whatever it holds. Empty when the URI
# had none. Its sessions are named openfga-migrate-goose, PGAPPNAME on its command alone (pgx maps it to
# application_name: pgconn/config.go:711 at v5.11.0), for the fence of step 2b.
query "SELECT format('lock held by pid %s, running openfga migrate', pg_backend_pid())"
log "$reply"
end_migration() {  # the EXIT trap while `openfga migrate` runs: this run takes the migration with it
  if kill "$migrate_pid" 2>/dev/null; then
    wait "$migrate_pid" 2>/dev/null || true
    log "killed openfga migrate (pid ${migrate_pid}): this run ends without it"
  fi
}
PGAPPNAME=openfga-migrate-goose OPENFGA_DATASTORE_URI="$db_uri" OPENFGA_DATASTORE_PASSWORD="${PGPASSWORD-}" "$OPENFGA_BINARY" migrate &
migrate_pid=$!
trap end_migration EXIT
while sleep "$probe_seconds" & probe_pid=$!; wait -n "$probe_pid" "$migrate_pid"; kill -0 "$migrate_pid" 2>/dev/null; do
  query "SELECT 1"
done
kill "$probe_pid" 2>/dev/null || true
wait "$probe_pid" 2>/dev/null || true
trap - EXIT
if wait "$migrate_pid"; then :; else
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
