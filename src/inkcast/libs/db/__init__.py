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
OutboxRepo         (the row's owner)   outbox, outbox_cursors (events written with the row;
                                       drained by InsightsService)
=================  ==================  =============================================
"""

from __future__ import annotations

from ..secrets import Sealer, SecretValue, new_secret, token_hash
from . import identity, ledger, outbox, posts, vault
from .database import Database
from .identity import IdentityRepo
from .ledger import LedgerRepo
from .outbox import OutboxRepo
from .posts import PostsRepo
from .vault import VaultRepo


class Store(IdentityRepo, VaultRepo, PostsRepo, LedgerRepo, OutboxRepo):
    SCHEMAS = (identity.SCHEMA, vault.SCHEMA, posts.SCHEMA, ledger.SCHEMA, outbox.SCHEMA)

    def __init__(self, path: str, secret_key: "str | SecretValue"):
        super().__init__(path)
        self.sealer = Sealer(secret_key)

    def purge_expired(self) -> None:
        self.purge_expired_identity()
        self.purge_expired_tickets()
        self.purge_outbox()


__all__ = ["Database", "IdentityRepo", "LedgerRepo", "OutboxRepo", "PostsRepo", "Sealer", "Store", "VaultRepo",
           "new_secret", "token_hash"]
