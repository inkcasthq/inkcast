# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Credential vault (owned by ConnectorService): sealed platform credentials and one-time connect tickets.

Credentials are encrypted with a key derived from INKCAST_SECRET_KEY; tickets are stored as hashes.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from ..secrets import Sealer, token_hash
from .database import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
  user_id TEXT NOT NULL, platform TEXT NOT NULL, sealed TEXT NOT NULL, account TEXT NOT NULL,
  updated_at REAL NOT NULL, PRIMARY KEY (user_id, platform)
);
CREATE TABLE IF NOT EXISTS connect_tickets (
  ticket_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, platform TEXT NOT NULL, expires_at REAL NOT NULL
);
"""


class VaultRepo(Database):
    sealer: Sealer

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

    def purge_expired_tickets(self) -> None:
        self._exec("DELETE FROM connect_tickets WHERE expires_at < ?", time.time())
