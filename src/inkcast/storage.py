# SPDX-License-Identifier: PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""SQLite persistence: users, OAuth state, encrypted platform credentials, posts.

Secrets are never stored in the clear: OAuth codes and tokens are stored as
SHA-256 hashes, platform credentials are encrypted with a key derived from
INKCAST_SECRET_KEY.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, display TEXT NOT NULL, email TEXT, name TEXT,
  created_at REAL NOT NULL, last_login_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_auth (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
  user_id TEXT, csrf TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
  code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, user_id TEXT NOT NULL,
  params TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, user_id TEXT NOT NULL,
  scopes TEXT NOT NULL, resource TEXT, family TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_family ON tokens(family);
CREATE TABLE IF NOT EXISTS credentials (
  user_id TEXT NOT NULL, platform TEXT NOT NULL, sealed TEXT NOT NULL, account TEXT NOT NULL,
  updated_at REAL NOT NULL, PRIMARY KEY (user_id, platform)
);
CREATE TABLE IF NOT EXISTS connect_tickets (
  ticket_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, platform TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL, platform TEXT NOT NULL,
  title TEXT NOT NULL, status TEXT NOT NULL, run_at REAL NOT NULL, payload TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, result_url TEXT, remote_id TEXT, error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_due ON posts(status, run_at);
CREATE INDEX IF NOT EXISTS posts_user ON posts(user_id, run_at);
"""


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


class Sealer:
    """Encrypts platform credentials at rest."""

    def __init__(self, secret_key: str):
        key = hashlib.sha256(("inkcast-credentials:" + secret_key).encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, data: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def unseal(self, sealed: str) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(sealed.encode()))
        except InvalidToken as exc:
            raise RuntimeError(
                "Stored credentials can't be decrypted; INKCAST_SECRET_KEY changed. Reconnect the platform."
            ) from exc


class Store:
    def __init__(self, path: str, secret_key: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        self.sealer = Sealer(secret_key)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _exec(self, sql: str, *args: Any) -> int:
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    # ---- users ------------------------------------------------------------

    def upsert_user(self, identity: str, display: str, email: str | None, name: str | None) -> dict[str, Any]:
        """``identity`` is the stable key from the identity provider, e.g. ``oidc:<issuer>#<sub>``."""
        now = time.time()
        with self._tx() as c:
            row = c.execute("SELECT * FROM users WHERE identity = ?", (identity,)).fetchone()
            if row:
                c.execute(
                    "UPDATE users SET display = ?, email = ?, name = ?, last_login_at = ? WHERE id = ?",
                    (display, email, name, now, row["id"]),
                )
                return {**dict(row), "display": display, "email": email, "name": name, "last_login_at": now}
            user = {"id": "usr_" + uuid.uuid4().hex[:16], "identity": identity, "display": display,
                    "email": email, "name": name, "created_at": now, "last_login_at": now}
            c.execute("INSERT INTO users VALUES (:id, :identity, :display, :email, :name, :created_at, :last_login_at)", user)
            return user

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM users WHERE id = ?", user_id)
        return dict(row) if row else None

    # ---- OAuth clients -------------------------------------------------------

    def save_client(self, client_id: str, info: str) -> None:
        self._exec("INSERT OR REPLACE INTO oauth_clients VALUES (?, ?, ?)", client_id, info, time.time())

    def get_client(self, client_id: str) -> str | None:
        row = self._one("SELECT info FROM oauth_clients WHERE client_id = ?", client_id)
        return row["info"] if row else None

    # ---- pending authorizations (between /authorize and the consent page) ----

    def create_pending(self, client_id: str, params: str, ttl: float = 900) -> str:
        pending_id = secrets.token_urlsafe(24)
        self._exec(
            "INSERT INTO pending_auth VALUES (?, ?, ?, NULL, ?, ?)",
            pending_id, client_id, params, secrets.token_urlsafe(24), time.time() + ttl,
        )
        return pending_id

    def get_pending(self, pending_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM pending_auth WHERE id = ? AND expires_at > ?", pending_id, time.time())
        return dict(row) if row else None

    def set_pending_user(self, pending_id: str, user_id: str) -> None:
        self._exec("UPDATE pending_auth SET user_id = ? WHERE id = ?", user_id, pending_id)

    def delete_pending(self, pending_id: str) -> None:
        self._exec("DELETE FROM pending_auth WHERE id = ?", pending_id)

    # ---- authorization codes and tokens --------------------------------------

    def save_code(self, code: str, client_id: str, user_id: str, params: str, ttl: float = 300) -> None:
        self._exec(
            "INSERT INTO auth_codes VALUES (?, ?, ?, ?, ?)", token_hash(code), client_id, user_id, params, time.time() + ttl
        )

    def get_code(self, code: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM auth_codes WHERE code_hash = ?", token_hash(code))
        return dict(row) if row else None

    def consume_code(self, code: str) -> bool:
        return self._exec("DELETE FROM auth_codes WHERE code_hash = ?", token_hash(code)) == 1

    def save_token(self, value: str, kind: str, client_id: str, user_id: str, scopes: list[str],
                   resource: str | None, family: str, ttl: float) -> int:
        expires_at = int(time.time() + ttl)
        self._exec(
            "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            token_hash(value), kind, client_id, user_id, " ".join(scopes), resource, family, expires_at,
        )
        return expires_at

    def get_token(self, value: str, kind: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM tokens WHERE token_hash = ? AND kind = ? AND expires_at > ?", token_hash(value), kind, time.time()
        )
        return dict(row) if row else None

    def revoke_family(self, family: str) -> None:
        self._exec("DELETE FROM tokens WHERE family = ?", family)

    def revoke_token(self, value: str) -> None:
        self._exec("DELETE FROM tokens WHERE token_hash = ?", token_hash(value))

    def purge_expired(self) -> None:
        now = time.time()
        with self._tx() as c:
            for table in ("pending_auth", "auth_codes", "tokens", "connect_tickets"):
                c.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,))

    # ---- platform credentials ---------------------------------------------

    def save_credentials(self, user_id: str, platform: str, creds: dict[str, Any], account: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO credentials VALUES (?, ?, ?, ?, ?)",
            user_id, platform, self.sealer.seal(creds), json.dumps(account), time.time(),
        )

    def get_credentials(self, user_id: str, platform: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        row = self._one("SELECT sealed, account FROM credentials WHERE user_id = ? AND platform = ?", user_id, platform)
        if not row:
            return None
        return self.sealer.unseal(row["sealed"]), json.loads(row["account"])

    def connected_accounts(self, user_id: str) -> dict[str, dict[str, Any]]:
        rows = self._all("SELECT platform, account FROM credentials WHERE user_id = ?", user_id)
        return {r["platform"]: json.loads(r["account"]) for r in rows}

    def delete_credentials(self, user_id: str, platform: str) -> bool:
        return self._exec("DELETE FROM credentials WHERE user_id = ? AND platform = ?", user_id, platform) == 1

    # ---- one-time links to the "connect a platform" page ------------------

    def create_ticket(self, user_id: str, platform: str, ttl: float = 900) -> str:
        ticket = secrets.token_urlsafe(32)
        self._exec("INSERT INTO connect_tickets VALUES (?, ?, ?, ?)", token_hash(ticket), user_id, platform, time.time() + ttl)
        return ticket

    def get_ticket(self, ticket: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM connect_tickets WHERE ticket_hash = ? AND expires_at > ?", token_hash(ticket), time.time()
        )
        return dict(row) if row else None

    def consume_ticket(self, ticket: str) -> None:
        self._exec("DELETE FROM connect_tickets WHERE ticket_hash = ?", token_hash(ticket))

    # ---- posts -------------------------------------------------------------

    def create_post(self, *, group_id: str, user_id: str, platform: str, title: str, status: str,
                    run_at: float, payload: dict[str, Any]) -> str:
        post_id = "pst_" + uuid.uuid4().hex[:12]
        now = time.time()
        self._exec(
            "INSERT INTO posts (id, group_id, user_id, platform, title, status, run_at, payload, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            post_id, group_id, user_id, platform, title, status, run_at, json.dumps(payload), now, now,
        )
        return post_id

    def update_post(self, post_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE posts SET {cols} WHERE id = ?", *fields.values(), post_id)

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM posts WHERE id = ?", post_id)
        return dict(row) if row else None

    def claim_due_posts(self, now: float, limit: int = 20) -> list[dict[str, Any]]:
        """Atomically move due scheduled posts to 'publishing' and return them."""
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM posts WHERE status = 'scheduled' AND run_at <= ? ORDER BY run_at LIMIT ?", (now, limit)
            ).fetchall()
            for r in rows:
                c.execute(
                    "UPDATE posts SET status = 'publishing', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                    (now, r["id"]),
                )
        return [{**dict(r), "attempts": r["attempts"] + 1} for r in rows]

    def recover_stuck(self, older_than: float) -> int:
        """Posts left 'publishing' by a crash go back to 'scheduled'."""
        return self._exec(
            "UPDATE posts SET status = 'scheduled' WHERE status = 'publishing' AND updated_at < ?", older_than
        )

    def ping(self) -> bool:
        self._one("SELECT 1")
        return True

    def post_counts(self) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM posts GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def due_count(self, now: float) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM posts WHERE status = 'scheduled' AND run_at <= ?", now)
        return int(row["n"]) if row else 0

    def list_posts(self, user_id: str, start: float, end: float) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM posts WHERE user_id = ? AND run_at BETWEEN ? AND ? ORDER BY run_at", user_id, start, end
        )
        return [dict(r) for r in rows]
