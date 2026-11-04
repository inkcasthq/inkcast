# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Backups of the SQLite database, until PostgreSQL (E3.F3.1.S5) brings base backups and point-in-time recovery.

* ``take_backup``: a consistent copy while the server keeps serving, through SQLite's online backup API (it copies
  a snapshot, WAL included; a plain file copy of a live WAL database can be torn). The copy is checked with
  ``PRAGMA integrity_check`` before it counts, written under a temporary name and renamed into place, and
  described by a manifest beside it (size, SHA-256, row counts per table). Backups ``keep`` or more days older than
  the one just taken are removed (by age, not by count, issue #112: an extra backup on the same day evicts no daily
  copy, and the first good backup after days without one removes everything that old at once); a failed backup never
  removes anything, so the alert on a missed backup (InkcastBackupMissing) covers the days in between.
* ``verify_backup``: the checksum against the manifest, then the integrity check.
* ``restore_backup``: verifies first, refuses while the scheduler's heartbeat is fresh (the server is still running),
  and never deletes the current database: it is moved aside as ``<db>.before-restore-<time>`` with its WAL files.
  That copy holds whoever is erased after the restore, so it ages out with the backups: ``take_backup`` removes it,
  with those files, once it is ``keep`` or more days old (issue #112), and ``aside_copies`` lists what is there.
  The ``user_erasures`` audit rows of the database being replaced are carried into the restored copy (issue #94): a
  backup taken before a person's erasure holds their rows and not the record of it, and that record is what lets
  the server erase them again when it next opens the store (``Store.reapply_erasures``). So are their
  ``user_erasure_aliases`` (issue #113), and the log line counts both. Erasures that only the replaced database knew
  of, when that database is lost too, are the one case this cannot cover; the runbook says so.

A backup is as sensitive as the database: platform credentials inside are encrypted with INKCAST_SECRET_KEY (keep
the key, or the backup can't be used, and never store the two together), and tokens are stored only as hashes.
Files are written 0640. Until they age out (``keep`` days, INKCAST_BACKUP_KEEP), backups hold an erased person's
data too; the deletion pages say so.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .erasure import SCHEMA as ERASURE_SCHEMA

log = logging.getLogger(__name__)

PREFIX = "inkcast-"
SUFFIX = ".db"
ASIDE = ".before-restore-"  # <db>.before-restore-<time>: the database a restore replaced, beside the restored one
STAMP = "%Y%m%dT%H%M%SZ"    # the UTC time in every copy's name: names sort by time, and say which day a copy is from
FRESH_HEARTBEAT_SECONDS = 120
DEFAULT_KEEP = 14           # days; the CronJob's INKCAST_BACKUP_KEEP and Config.backup_keep say the same


class BackupError(Exception):
    """A backup or restore that must not count: the reason, in one sentence."""


@dataclass(frozen=True)
class Backup:
    path: pathlib.Path
    manifest: dict
    removed_backups: int = 0    # backups ``keep`` or more days older than this one, removed once it was good
    removed_copies: int = 0     # the copies restores left beside the database (``ASIDE``) that old, removed too

    @property
    def name(self) -> str:
        return self.path.name


def _stamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime(STAMP)


def _stamped(text: str) -> datetime | None:
    """The time a copy's name carries, or None for a name that carries none: retention leaves such a file alone."""
    try:
        return datetime.strptime(text, STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _aged_out(stamp: datetime, now: float, keep: int) -> bool:
    """Whether a copy from ``stamp`` is ``keep`` or more days old on the day of ``now``. Days are UTC dates, so the
    daily backup removes the one taken ``keep`` days earlier whatever the minutes between the two runs, an extra
    backup on the same day evicts nothing, and the copy just taken (``keep`` is 1 or more) is never removed."""
    today = datetime.fromtimestamp(now, timezone.utc).date()
    return (today - stamp.date()).days >= keep


def _prune_backups(directory: pathlib.Path, keep: int, now: float) -> int:
    """Remove the backups in ``directory`` that are ``keep`` or more days old, with their manifests; how many went.
    What a backup killed midway left goes by the same rule: a backup renamed into place without its manifest (counted)
    and a ``.partial`` copy (a SIGTERM at the Job's deadline runs no cleanup), each a whole copy (issue #112)."""
    removed = 0
    for old in sorted(directory.glob(f"{PREFIX}*{SUFFIX}")):     # with or without its manifest
        stamp = _stamped(old.name[len(PREFIX):-len(SUFFIX)])
        if stamp is not None and _aged_out(stamp, now, keep):
            old.unlink(missing_ok=True)
            _manifest_path(old).unlink(missing_ok=True)
            removed += 1
    for partial in directory.glob(f".{PREFIX}*{SUFFIX}.partial"):
        stamp = _stamped(partial.name[len(PREFIX) + 1:-len(SUFFIX + ".partial")])
        if stamp is not None and _aged_out(stamp, now, keep):
            partial.unlink(missing_ok=True)
    return removed


def aside_copies(database: pathlib.Path) -> list[pathlib.Path]:
    """The copies restores left beside ``database`` (``<db>.before-restore-<time>``), oldest first: the files whose
    name carries a time, not their WAL files, and not a copy someone named by hand."""
    prefix = database.name + ASIDE
    return sorted(p for p in database.parent.glob(f"{prefix}*") if _stamped(p.name[len(prefix):]) is not None)


def _content_time(database: pathlib.Path, now: float) -> float:
    """When the content of ``database`` is from: ``now``, or for a restore's copy (``<db>.before-restore-<time>``) the
    time of that restore. A backup of the copy (the runbook's way back to it) is named with that time, so it ages out
    when the copy would have: the copy holds whoever was erased after the restore (issue #112)."""
    base, _, rest = database.name.rpartition(ASIDE)
    stamp = _stamped(rest) if base else None
    return stamp.timestamp() if stamp else now


def _prune_copies(database: pathlib.Path, keep: int, now: float) -> int:
    """Remove the copies restores left beside ``database`` that are ``keep`` or more days old, each with its WAL files
    (the restore moved them aside with it); how many copies went."""
    removed = 0
    for copy in aside_copies(database):
        if _aged_out(_stamped(copy.name[len(database.name + ASIDE):]), now, keep):
            for extra in ("", "-wal", "-shm"):
                copy.with_name(copy.name + extra).unlink(missing_ok=True)
            removed += 1
    return removed


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: pathlib.Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return ",".join(r[0] for r in conn.execute("PRAGMA integrity_check").fetchall())
    finally:
        conn.close()


def _row_counts(path: pathlib.Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


def _manifest_path(backup: pathlib.Path) -> pathlib.Path:
    return backup.with_suffix(".json")


def list_backups(directory: pathlib.Path) -> list[pathlib.Path]:
    """Completed backups in ``directory``, oldest first (names sort by time)."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(f"{PREFIX}*{SUFFIX}") if _manifest_path(p).exists())


def take_backup(database: pathlib.Path, directory: pathlib.Path, *, keep: int = DEFAULT_KEEP, version: str = "",
                now: float | None = None) -> Backup:
    if not database.exists():
        raise BackupError(f"No database at {database}.")
    if keep < 1:
        raise BackupError("keep must be at least 1.")
    directory.mkdir(parents=True, exist_ok=True)
    now = now or time.time()
    taken = _content_time(database, now)
    if _aged_out(datetime.fromtimestamp(taken, timezone.utc), now, keep):
        raise BackupError(f"{database.name} is {keep} or more days old: it has aged out, and a backup of it would too.")
    stamp = _stamp(taken)
    target = directory / f"{PREFIX}{stamp}{SUFFIX}"
    if target.exists():
        raise BackupError(f"{target.name} already exists; one backup per second at most.")
    partial = directory / f".{target.name}.partial"
    old_umask = os.umask(0o027)
    try:
        # The live database is opened normally (a read-only open of a WAL database fails when its -shm can't be
        # made); only reads happen on it.
        source = sqlite3.connect(database, timeout=30)
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)                         # a consistent snapshot, page by page, WAL included
            # The copy inherits the WAL flag from page 1; a backup is one self-contained file, so switch it to a
            # rollback journal (it then opens read-only anywhere, with no -wal or -shm beside it).
            copy.execute("PRAGMA journal_mode=DELETE")
        finally:
            copy.close()
            source.close()
        result = _integrity(partial)
        if result != "ok":
            raise BackupError(f"The copy failed its integrity check ({result[:200]}); nothing was kept.")
        with partial.open("rb+") as f:
            os.fsync(f.fileno())
        manifest = {
            "file": target.name, "created_at": stamp, "source": str(database), "size": partial.stat().st_size,
            "sha256": _sha256(partial), "integrity": "ok", "sqlite": sqlite3.sqlite_version,
            "inkcast": version, "rows": _row_counts(partial),
        }
        partial.replace(target)
        _manifest_path(target).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        os.umask(old_umask)
    # Retention, only after a good backup: by age, so the promise "within keep days" holds from the first good backup
    # after any run of failed ones, and a backup taken by hand evicts no daily copy. The copies restores left beside
    # the database go by the same rule: they hold whoever was erased after the restore (issue #112).
    return Backup(target, manifest, removed_backups=_prune_backups(directory, keep, now),
                  removed_copies=_prune_copies(database, keep, now))


def verify_backup(path: pathlib.Path) -> dict:
    manifest_file = _manifest_path(path)
    if not path.exists() or not manifest_file.exists():
        raise BackupError(f"{path} or its manifest is missing.")
    manifest = json.loads(manifest_file.read_text())
    if _sha256(path) != manifest.get("sha256"):
        raise BackupError(f"{path.name} doesn't match its manifest's checksum: it changed after it was taken.")
    result = _integrity(path)
    if result != "ok":
        raise BackupError(f"{path.name} fails the integrity check ({result[:200]}).")
    return manifest


def _heartbeat_age(database: pathlib.Path, now: float) -> float | None:
    """Seconds since the freshest heartbeat in the current database, or None when there is none."""
    if not database.exists():
        return None
    try:
        conn = sqlite3.connect(database, timeout=10)     # a normal open: a read-only one can fail on a WAL database
        try:
            row = conn.execute("SELECT MAX(at) FROM heartbeats").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None                                 # a database that never ran a scheduler
        raise BackupError(f"Couldn't read the current database to see whether the server runs: {exc}") from exc
    return None if not row or row[0] is None else now - float(row[0])


def _carry_erasures(current: pathlib.Path, incoming: pathlib.Path) -> tuple[int, int]:
    """Copy the ``user_erasures`` rows of the database being replaced, and their ``user_erasure_aliases``, into the
    restored copy, skipping any it already has; how many records and how many aliases were carried (the restore logs
    both: a backup that holds every record can still lack an alias, #183). Zeros when there is no current database or
    it predates the table. A current database that can't be read refuses the restore (the heartbeat read above has
    already needed it), before anything is swapped."""
    if not current.exists():
        return 0, 0
    source = sqlite3.connect(current, timeout=10)
    try:
        try:
            rows = source.execute("SELECT user_hash, erased_at, counts FROM user_erasures").fetchall()
            aliases = _erasure_aliases(source)          # the other hash an erasure is known by (issue #113)
        except sqlite3.DatabaseError as exc:
            if "no such table" in str(exc):
                return 0, 0
            raise BackupError(f"Couldn't read the current database's erasure records: {exc}") from exc
    finally:
        source.close()
    if not rows:
        return 0, 0
    target = sqlite3.connect(incoming)
    try:
        target.executescript(ERASURE_SCHEMA)                # a backup from before the table existed gets it
        have = set(target.execute("SELECT user_hash, erased_at FROM user_erasures").fetchall())
        new = []
        for h, t, c in rows:                                # one pass: a NOT EXISTS per row is quadratic
            if (h, t) not in have:
                have.add((h, t))
                new.append((h, t, c))
        target.executemany("INSERT INTO user_erasures VALUES (?, ?, ?)", new)
        known = set(_erasure_aliases(target))
        new_aliases = [a for a in dict.fromkeys(aliases) if a not in known]
        target.executemany("INSERT INTO user_erasure_aliases VALUES (?, ?)", new_aliases)
        target.commit()
        return len(new), len(new_aliases)
    finally:
        target.close()


def _erasure_aliases(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """``user_erasure_aliases`` as (user_hash, alias) pairs; none in a database from before the table."""
    try:
        return [tuple(r) for r in conn.execute("SELECT user_hash, alias FROM user_erasure_aliases")]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


def restore_backup(path: pathlib.Path, database: pathlib.Path, *, force: bool = False,
                   now: float | None = None) -> pathlib.Path | None:
    """Put ``path`` in place of ``database``. Returns where the previous database was moved, if there was one."""
    now = now or time.time()
    manifest = verify_backup(path)
    age = _heartbeat_age(database, now)
    if age is not None and age < FRESH_HEARTBEAT_SECONDS and not force:
        raise BackupError(f"The server looks alive (a heartbeat {age:.0f} s ago): stop it first (scale to 0), "
                          "or pass --force if it is really stopped.")
    database.parent.mkdir(parents=True, exist_ok=True)
    incoming = database.with_name(f".{database.name}.restoring")
    old_umask = os.umask(0o027)
    try:
        src, dst = sqlite3.connect(f"file:{path}?mode=ro", uri=True), sqlite3.connect(incoming)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    finally:
        os.umask(old_umask)
    if _integrity(incoming) != "ok" or _row_counts(incoming) != manifest.get("rows", _row_counts(incoming)):
        incoming.unlink(missing_ok=True)
        raise BackupError("The restored copy doesn't match the backup; the current database was not touched.")
    try:
        carried, aliases = _carry_erasures(database, incoming)     # after the copy is checked, before it is swapped in
    except BaseException:
        incoming.unlink(missing_ok=True)
        raise
    if carried or aliases:
        log.info("Carried %d erasure record(s) and %d alias(es) from the current database into the restored copy: the "
                 "server erases those people again when it starts", carried, aliases,
                 extra={"event": "restore.erasures.carried", "rows": carried, "aliases": aliases})
    aside = None
    if database.exists():
        aside = database.with_name(f"{database.name}{ASIDE}{_stamp(now)}")   # ages out: aside_copies, take_backup
        os.link(database, aside)                        # a second name: the old file stays at its path until replaced
        for extra in ("-wal", "-shm"):                  # the old WAL belongs to the old file, never the restored one
            side = database.with_name(database.name + extra)
            if side.exists():
                side.replace(aside.with_name(aside.name + extra))
    incoming.replace(database)                          # one rename: a kill leaves the old database or the new one
    return aside
