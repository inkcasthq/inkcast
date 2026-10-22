# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Identity data (owned by IdentityService): users, OAuth clients and grants, personal access tokens.

Secrets are never stored in the clear: codes and tokens are stored as SHA-256 hashes.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Any

from ..secrets import token_hash
from .database import Database

SCHEMA = """
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
CREATE TABLE IF NOT EXISTS api_tokens (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
  scopes TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL, last_used_at REAL, revoked_at REAL
);
CREATE INDEX IF NOT EXISTS api_tokens_user ON api_tokens(user_id);
-- Small named values a worker keeps between runs (the Keycloak sync's event cursor).
CREATE TABLE IF NOT EXISTS sync_state (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
-- Delete my account (E9.F9.5.S4): one row per request. link_hash is the one-time link (NULL once opened);
-- cookie_hash and keycloak_id are set when the person continues to Keycloak, for the visit that comes back. The row
-- outlives erase_user (the sync may erase first) and goes when that visit is answered, or with its TTL.
CREATE TABLE IF NOT EXISTS deletion_requests (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, link_hash TEXT UNIQUE, cookie_hash TEXT UNIQUE, keycloak_id TEXT,
  created_at REAL NOT NULL, expires_at REAL NOT NULL
);
"""
# disabled_at: set when the identity provider disabled or deleted the person (E9.F9.5); cleared at their next
# successful sign-in, which the provider only allows once they are enabled again.
COLUMNS = (("users", "disabled_at", "REAL"),)


# Defence in depth behind disable_user() and erase_user(): a token or code minted in the moment a person was being
# cut off or erased (a sign-in finishing as Keycloak disabled them, or as their account was deleted) is refused
# anyway: only a token whose person exists and is not disabled is honoured.
_ACTIVE_USER = " AND EXISTS (SELECT 1 FROM users WHERE users.id = user_id AND users.disabled_at IS NULL)"  # by key


class IdentityRepo(Database):
    # ---- users ------------------------------------------------------------

    def upsert_user(self, identity: str, display: str, email: str | None, name: str | None) -> dict[str, Any]:
        """``identity`` is the stable key from the identity provider, e.g. ``oidc:<issuer>#<sub>``."""
        now = time.time()
        with self._tx() as c:
            row = c.execute("SELECT * FROM users WHERE identity = ?", (identity,)).fetchone()
            if row:
                c.execute(
                    "UPDATE users SET display = ?, email = ?, name = ?, last_login_at = ?, disabled_at = NULL WHERE id = ?",
                    (display, email, name, now, row["id"]),
                )
                return {**dict(row), "display": display, "email": email, "name": name, "last_login_at": now,
                        "disabled_at": None}
            user = {"id": "usr_" + uuid.uuid4().hex[:16], "identity": identity, "display": display,
                    "email": email, "name": name, "created_at": now, "last_login_at": now, "disabled_at": None}
            c.execute("INSERT INTO users (id, identity, display, email, name, created_at, last_login_at, disabled_at)"
                      " VALUES (:id, :identity, :display, :email, :name, :created_at, :last_login_at, :disabled_at)", user)
            return user

    def find_user_by_identity(self, identity: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM users WHERE identity = ?", identity)
        return dict(row) if row else None

    def users_with_identity_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Users from one identity provider (e.g. ``oidc:<issuer>#``) who are not disabled."""
        rows = self._all("SELECT * FROM users WHERE substr(identity, 1, ?) = ? AND disabled_at IS NULL ORDER BY id",
                         len(prefix), prefix)
        return [dict(r) for r in rows]

    def disable_user(self, user_id: str) -> dict[str, int]:
        """The person can no longer use Inkcast: every OAuth token, code and personal access token goes, in one
        transaction. Counts of what was cut off; zeros when they were already disabled."""
        now = time.time()
        with self._tx() as c:
            if c.execute("UPDATE users SET disabled_at = ? WHERE id = ? AND disabled_at IS NULL",
                         (now, user_id)).rowcount != 1:
                return {"oauth_tokens": 0, "personal_access_tokens": 0, "codes": 0}
            tokens = c.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,)).rowcount
            codes = c.execute("DELETE FROM auth_codes WHERE user_id = ?", (user_id,)).rowcount
            pats = c.execute("UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                             (now, user_id)).rowcount
        return {"oauth_tokens": tokens, "personal_access_tokens": pats, "codes": codes}

    def get_sync_state(self, name: str) -> str | None:
        row = self._one("SELECT value FROM sync_state WHERE name = ?", name)
        return row["value"] if row else None

    def set_sync_state(self, name: str, value: str) -> None:
        self._exec("INSERT INTO sync_state VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value, "
                   "updated_at = excluded.updated_at", name, value, time.time())

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

    # ---- authorization codes and OAuth tokens --------------------------------

    def save_code(self, code: str, client_id: str, user_id: str, params: str, ttl: float = 300) -> None:
        self._exec(
            "INSERT INTO auth_codes VALUES (?, ?, ?, ?, ?)", token_hash(code), client_id, user_id, params, time.time() + ttl
        )

    def get_code(self, code: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM auth_codes WHERE code_hash = ?" + _ACTIVE_USER, token_hash(code))
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
            "SELECT * FROM tokens WHERE token_hash = ? AND kind = ? AND expires_at > ?" + _ACTIVE_USER,
            token_hash(value), kind, time.time()
        )
        return dict(row) if row else None

    def rotate_refresh(self, value: str, client_id: str) -> dict[str, Any] | None:
        """Consume a refresh token and revoke its whole family in one transaction; the row, or None.

        Only one caller can win: two refreshes with the same token are serialised by the write lock, and the
        second finds nothing (so a stolen-and-replayed refresh token can't mint a second family). On PostgreSQL
        (E3) the same shape is SELECT … FOR UPDATE, then DELETE, in one transaction."""
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM tokens WHERE token_hash = ? AND kind = 'refresh' AND client_id = ? AND expires_at > ?",
                (token_hash(value), client_id, time.time()),
            ).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM tokens WHERE family = ?", (row["family"],))
        return dict(row)

    def revoke_family(self, family: str) -> None:
        self._exec("DELETE FROM tokens WHERE family = ?", family)

    def revoke_token(self, value: str) -> None:
        self._exec("DELETE FROM tokens WHERE token_hash = ?", token_hash(value))

    # ---- personal access tokens (ink_pat_<id>_<secret>) -----------------------

    def create_api_token(self, user_id: str, name: str, scopes: list[str], expires_at: float | None) -> tuple[str, dict[str, Any]]:
        """Return (the token, shown once; its row). Only the hash is stored."""
        token_id = "tok_" + secrets.token_hex(6)
        token = f"ink_pat_{token_id[4:]}_{secrets.token_urlsafe(32)}"
        row = {"id": token_id, "user_id": user_id, "name": name, "token_hash": token_hash(token),
               "scopes": " ".join(scopes), "created_at": time.time(), "expires_at": expires_at,
               "last_used_at": None, "revoked_at": None}
        self._exec("INSERT INTO api_tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", *row.values())
        return token, row

    def find_api_token(self, token: str) -> dict[str, Any] | None:
        """The active token row for a presented token (not revoked, not expired); records its use."""
        now = time.time()
        row = self._one("SELECT * FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL"
                        " AND (expires_at IS NULL OR expires_at > ?)" + _ACTIVE_USER, token_hash(token), now)
        if not row:
            return None
        if not row["last_used_at"] or now - row["last_used_at"] > 60:  # don't write on every request
            self._exec("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", now, row["id"])
        return dict(row)

    def list_api_tokens(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM api_tokens WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at", user_id)
        return [dict(r) for r in rows]

    def revoke_api_token(self, user_id: str, token_id: str) -> bool:
        return self._exec("UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                          time.time(), token_id, user_id) == 1

    # ---- delete my account (E9.F9.5.S4): the one-time link, then the visit that comes back from Keycloak ------

    def create_deletion_request(self, user_id: str, ttl: float) -> str:
        """A one-time link ticket for the person's own deletion page; only its hash is stored."""
        ticket = secrets.token_urlsafe(32)
        now = time.time()
        self._exec("INSERT INTO deletion_requests (id, user_id, link_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                   "del_" + uuid.uuid4().hex[:16], user_id, token_hash(ticket), now, now + ttl)
        return ticket

    def open_deletion_link(self, ticket: str) -> dict[str, Any] | None:
        """The request behind a link, consuming the link (a second visit finds nothing); None when unknown or expired."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM deletion_requests WHERE link_hash = ? AND expires_at > ?",
                            (token_hash(ticket), time.time())).fetchone()
            if not row:
                return None
            c.execute("UPDATE deletion_requests SET link_hash = NULL WHERE id = ?", (row["id"],))
        return dict(row)

    def get_deletion_request(self, request_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM deletion_requests WHERE id = ? AND expires_at > ?", request_id, time.time())
        return dict(row) if row else None

    def arm_deletion_return(self, request_id: str, keycloak_id: str, ttl: float) -> str:
        """The person is being sent to Keycloak: remember who, for the visit that comes back. Returns the cookie
        value (only its hash is stored); the request now lives ``ttl`` more seconds."""
        cookie = secrets.token_urlsafe(32)
        self._exec("UPDATE deletion_requests SET cookie_hash = ?, keycloak_id = ?, expires_at = ? WHERE id = ?",
                   token_hash(cookie), keycloak_id, time.time() + ttl, request_id)
        return cookie

    def deletion_request_by_cookie(self, cookie: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM deletion_requests WHERE cookie_hash = ? AND expires_at > ?", token_hash(cookie),
                        time.time())
        return dict(row) if row else None

    def delete_deletion_request(self, request_id: str) -> None:
        self._exec("DELETE FROM deletion_requests WHERE id = ?", request_id)

    def purge_expired_identity(self) -> None:
        now = time.time()
        with self._tx() as c:
            for table in ("pending_auth", "auth_codes", "tokens", "deletion_requests"):
                c.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,))
