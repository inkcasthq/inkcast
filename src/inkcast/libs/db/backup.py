# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Backups of the SQLite database, until PostgreSQL (E3.F3.1.S5) brings base backups and point-in-time recovery.

* ``take_backup``: a consistent copy while the server keeps serving, through SQLite's online backup API (it copies
  a snapshot, WAL included; a plain file copy of a live WAL database can be torn). The copy is checked with
  ``PRAGMA integrity_check`` before it counts, written under a temporary name and renamed into place, and
  described by a manifest beside it (size, SHA-256, row counts per table). Old backups beyond ``keep`` are removed,
  oldest first; a failed backup never removes anything.
* ``verify_backup``: the checksum against the manifest, then the integrity check.
* ``restore_backup``: verifies first, refuses while the scheduler's heartbeat is fresh (the server is still running),
  and never deletes the current database: it is moved aside as ``<db>.before-restore-<time>`` with its WAL files.
  The ``user_erasures`` audit rows of the database being replaced are carried into the restored copy (issue #94): a
  backup taken before a person's erasure holds their rows and not the record of it, and that record is what lets
  the server erase them again when it next opens the store (``Store.reapply_erasures``). Erasures that only the
  replaced database knew of, when that database is lost too, are the one case this cannot cover; the runbook says so.

A backup is as sensitive as the database: platform credentials inside are encrypted with INKCAST_SECRET_KEY (keep
the key, or the backup can't be used, and never store the two together), and tokens are stored only as hashes.
Files are written 0640. Until they age out (``keep`` daily copies, INKCAST_BACKUP_KEEP), backups hold an erased
person's data too; the deletion pages say so.
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
FRESH_HEARTBEAT_SECONDS = 120
DEFAULT_KEEP = 14           # daily copies; the CronJob's INKCAST_BACKUP_KEEP and Config.backup_keep say the same


class BackupError(Exception):
    """A backup or restore that must not count: the reason, in one sentence."""


@dataclass(frozen=True)
class Backup:
    path: pathlib.Path
    manifest: dict

    @property
    def name(self) -> str:
        return self.path.name


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
    stamp = datetime.fromtimestamp(now or time.time(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
    for old in list_backups(directory)[:-keep]:         # retention, oldest first, only after a good backup
        old.unlink(missing_ok=True)
        _manifest_path(old).unlink(missing_ok=True)
    return Backup(target, manifest)


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


def _carry_erasures(current: pathlib.Path, incoming: pathlib.Path) -> int:
    """Copy the ``user_erasures`` rows of the database being replaced into the restored copy, skipping any it already
    has; how many were carried. Zero when there is no current database or it predates the table. A current database
    that can't be read refuses the restore (the heartbeat read above has already needed it), before anything is
    swapped."""
    if not current.exists():
        return 0
    source = sqlite3.connect(current, timeout=10)
    try:
        try:
            rows = source.execute("SELECT user_hash, erased_at, counts FROM user_erasures").fetchall()
        except sqlite3.DatabaseError as exc:
            if "no such table" in str(exc):
                return 0
            raise BackupError(f"Couldn't read the current database's erasure records: {exc}") from exc
    finally:
        source.close()
    if not rows:
        return 0
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
        target.commit()
        return target.total_changes
    finally:
        target.close()


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
        carried = _carry_erasures(database, incoming)       # after the copy is checked, before it is swapped in
    except BaseException:
        incoming.unlink(missing_ok=True)
        raise
    if carried:
        log.info("Carried %d erasure record(s) from the current database into the restored copy: the server erases "
                 "those people again when it starts", carried, extra={"event": "restore.erasures.carried", "rows": carried})
    aside = None
    if database.exists():
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        aside = database.with_name(f"{database.name}.before-restore-{stamp}")
        database.replace(aside)
        for extra in ("-wal", "-shm"):                  # the old WAL belongs to the old file, never the restored one
            side = database.with_name(database.name + extra)
            if side.exists():
                side.replace(aside.with_name(aside.name + extra))
    incoming.replace(database)
    return aside
