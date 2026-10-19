# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Authorization tuples for the local policy engine (E3.F3.6.S3): ``user has relation on object``.

Only ``LocalEngine`` reads and writes this table; with ``INKCAST_AUTHZ_ENGINE=openfga`` it stays empty and
OpenFGA holds the tuples. Rows are plain strings in OpenFGA's spelling (``user:usr_1``,
``organization:acme#member``, ``post:post_1``), so a store can be exported to OpenFGA tuple by tuple.
The column is ``subject`` rather than ``user`` because ``user`` is a reserved word in PostgreSQL (ADR-0014).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Iterable

from .database import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS authz_tuples (
  subject TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL, object_type TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (subject, relation, object)
);
-- The evaluator's two lookups: the users of (object, relation), and every object of a type (list_objects).
CREATE INDEX IF NOT EXISTS authz_tuples_object ON authz_tuples(object, relation);
CREATE INDEX IF NOT EXISTS authz_tuples_type ON authz_tuples(object_type, object);
"""

Triple = tuple[str, str, str]  # (user, relation, object)


class TupleConflict(ValueError):
    """A write of a tuple that exists, or a delete of one that doesn't (OpenFGA's rule); nothing was changed."""


class AuthzRepo(Database):
    def authz_users_of(self, obj: str, relation: str) -> list[str]:
        rows = self._all("SELECT subject FROM authz_tuples WHERE object = ? AND relation = ? ORDER BY subject",
                         obj, relation)
        return [r["subject"] for r in rows]

    def authz_objects_of_type(self, type_: str) -> list[str]:
        rows = self._all("SELECT DISTINCT object FROM authz_tuples WHERE object_type = ? ORDER BY object", type_)
        return [r["object"] for r in rows]

    def authz_write(self, writes: Iterable[Triple] = (), deletes: Iterable[Triple] = ()) -> None:
        """All or nothing, in one transaction. Deletes first, so a request may replace a tuple it also removes."""
        now = time.time()
        with self._tx() as c:
            for user, relation, obj in deletes:
                if c.execute("DELETE FROM authz_tuples WHERE subject = ? AND relation = ? AND object = ?",
                             (user, relation, obj)).rowcount != 1:
                    raise TupleConflict(f"cannot delete a tuple which does not exist: {user} {relation} {obj}")
            for user, relation, obj in writes:
                try:
                    c.execute("INSERT INTO authz_tuples VALUES (?, ?, ?, ?, ?)",
                              (user, relation, obj, obj.partition(":")[0], now))
                except sqlite3.IntegrityError:
                    raise TupleConflict(f"cannot write a tuple which already exists: {user} {relation} {obj}") from None

    def authz_read(self, user: str | None = None, relation: str | None = None, obj: str | None = None,
                   type_: str | None = None) -> list[Triple]:
        where, args = [], []
        for column, value in (("subject", user), ("relation", relation), ("object", obj), ("object_type", type_)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        sql = "SELECT subject, relation, object FROM authz_tuples"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._all(sql + " ORDER BY object, relation, subject", *args)
        return [(r["subject"], r["relation"], r["object"]) for r in rows]

    def authz_tuple_count(self) -> int:
        row = self._one("SELECT count(*) AS n FROM authz_tuples")
        return int(row["n"]) if row else 0
