# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Persistence shared library.

``Store`` is the union of the domain repositories over one connection. Each repository is
owned (written) by one service:

=================  ==================  =============================================
Repository         Owner               Tables
=================  ==================  =============================================
IdentityRepo       IdentityService     users, oauth_clients, pending_auth, auth_codes,
                                       tokens, api_tokens
VaultRepo          ConnectorService    credentials, connect_tickets
PostsRepo          PublishingApi       posts, heartbeats (scheduler writes status)
LedgerRepo         PublishingApi       ledger
=================  ==================  =============================================
"""

from __future__ import annotations

from ..secrets import Sealer, SecretValue, new_secret, token_hash
from . import identity, ledger, posts, vault
from .database import Database
from .identity import IdentityRepo
from .ledger import LedgerRepo
from .posts import PostsRepo
from .vault import VaultRepo


class Store(IdentityRepo, VaultRepo, PostsRepo, LedgerRepo):
    SCHEMAS = (identity.SCHEMA, vault.SCHEMA, posts.SCHEMA, ledger.SCHEMA)

    def __init__(self, path: str, secret_key: "str | SecretValue"):
        super().__init__(path)
        self.sealer = Sealer(secret_key)

    def purge_expired(self) -> None:
        self.purge_expired_identity()
        self.purge_expired_tickets()


__all__ = ["Database", "IdentityRepo", "LedgerRepo", "PostsRepo", "Sealer", "Store", "VaultRepo",
           "new_secret", "token_hash"]
