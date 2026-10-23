# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""`inkcast backup` and `inkcast restore`: the SQLite database, until PostgreSQL brings its own backups (E3).

    inkcast backup  [--database PATH] [--to DIR] [--keep N]
    inkcast restore (BACKUP | --latest DIR) [--database PATH] [--force]
    inkcast backups [--to DIR]                      # list, newest last, with size and checksum

The database defaults to INKCAST_DATABASE, the directory to INKCAST_BACKUP_DIR (or ./backups). A backup runs while
the server serves; a restore refuses while the server is running (stop it first). Exit status: 0 done, 1 failed,
2 bad usage. Each outcome is one log line with an event (`backup.done`, `backup.failed`, `restore.done`,
`restore.refused`). Runbook: docs/runbooks/backup-restore.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys

from . import __version__
from .libs.config import ConfigError, backup_keep_from, backup_keep_problems
from .libs.db.backup import BackupError, list_backups, restore_backup, take_backup, verify_backup
from .libs.secrets.checks import backup_keep_warning
from .libs.observability import setup_logging

log = logging.getLogger("inkcast.backup")


def _keep(raw: str) -> int:
    """``--keep``, under the same rule as INKCAST_BACKUP_KEEP (libs/config): a whole number, 1 or more."""
    try:
        keep = int(raw)
    except ValueError:
        keep = -1
    if backup_keep_problems(keep, got=raw):
        raise argparse.ArgumentTypeError(f"must be a whole number, 1 or more (got {raw!r}).")
    return keep


def _defaults() -> tuple[pathlib.Path, pathlib.Path]:
    return (pathlib.Path(os.environ.get("INKCAST_DATABASE", "./inkcast.db")),
            pathlib.Path(os.environ.get("INKCAST_BACKUP_DIR", "./backups")))


def _logging() -> None:
    setup_logging(os.environ.get("INKCAST_LOG_FORMAT", "text").lower(), os.environ.get("INKCAST_LOG_LEVEL", "INFO"),
                  service="Backup")


def backup_main(argv: list[str]) -> int:
    database, directory = _defaults()
    parser = argparse.ArgumentParser(prog="inkcast backup", description="Back up the database while it serves.")
    parser.add_argument("--database", type=pathlib.Path, default=database)
    parser.add_argument("--to", type=pathlib.Path, default=directory, help="directory for the backups")
    parser.add_argument("--keep", type=_keep, default=None, metavar="N",
                        help="how many backups to keep, 1 or more (oldest removed first, only after a good backup); "
                             "INKCAST_BACKUP_KEEP, or 14, when not given; above 35 warns, as the server does")
    args = parser.parse_args(argv)
    setting = "--keep" if args.keep is not None else "INKCAST_BACKUP_KEEP"
    if args.keep is None:
        try:
            args.keep = backup_keep_from(os.environ)       # the CronJob's value, under the server's rule (libs/config)
        except ConfigError as exc:
            parser.error(str(exc))                         # exit 2, bad usage: the variable named, no traceback
    _logging()
    warning = backup_keep_warning(args.keep, setting)      # the server's posture check (backup-keep), for the CronJob too
    if warning:
        log.warning("Posture: Backups age out within weeks: %s", warning,
                    extra={"event": "config.posture.warn", "check": "backup-keep"})
    try:
        b = take_backup(args.database, args.to, keep=args.keep, version=__version__)
    except (BackupError, OSError) as exc:
        log.error("Backup failed: %s", exc, extra={"event": "backup.failed", "database": str(args.database)})
        return 1
    log.info("Backup %s: %d bytes, sha256 %s, %d tables", b.name, b.manifest["size"], b.manifest["sha256"][:12],
             len(b.manifest["rows"]), extra={"event": "backup.done", "backup": b.name, "size": b.manifest["size"],
                                              "rows": sum(b.manifest["rows"].values())})
    return 0


def restore_main(argv: list[str]) -> int:
    database, _ = _defaults()
    parser = argparse.ArgumentParser(prog="inkcast restore", description="Replace the database with a backup.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("backup", nargs="?", type=pathlib.Path, help="the backup file (inkcast-<time>.db)")
    which.add_argument("--latest", type=pathlib.Path, metavar="DIR", help="the newest backup in DIR")
    parser.add_argument("--database", type=pathlib.Path, default=database)
    parser.add_argument("--force", action="store_true", help="restore even though the server's heartbeat is fresh")
    args = parser.parse_args(argv)
    _logging()
    path = args.backup
    if args.latest:
        found = list_backups(args.latest)
        if not found:
            log.error("No backups in %s.", args.latest, extra={"event": "restore.refused"})
            return 1
        path = found[-1]
    try:
        aside = restore_backup(path, args.database, force=args.force)
    except (BackupError, OSError) as exc:
        log.error("Restore refused: %s", exc, extra={"event": "restore.refused", "backup": str(path)})
        return 1
    log.info("Restored %s into %s%s", path.name, args.database,
             f"; the previous database is kept as {aside.name}" if aside else "",
             extra={"event": "restore.done", "backup": path.name})
    return 0


def list_main(argv: list[str]) -> int:
    _, directory = _defaults()
    parser = argparse.ArgumentParser(prog="inkcast backups", description="List the backups, newest last.")
    parser.add_argument("--to", type=pathlib.Path, default=directory)
    args = parser.parse_args(argv)
    for path in list_backups(args.to):
        try:
            m = verify_backup(path)
            print(f"{path.name}  {m['size']:>10} bytes  sha256 {m['sha256'][:12]}  ok")
        except BackupError as exc:
            print(f"{path.name}  BROKEN: {exc}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(backup_main(sys.argv[1:]))
