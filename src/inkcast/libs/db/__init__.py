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
KpiRepo            InsightsService     kpi_rollups, kpi_applied
=================  ==================  =============================================
"""

from __future__ import annotations

from ..secrets import Sealer, SecretValue, new_secret, token_hash
from . import identity, kpi, ledger, outbox, posts, vault
from .database import Database
from .identity import IdentityRepo
from .kpi import KpiRepo
from .ledger import LedgerRepo
from .outbox import OutboxRepo
from .posts import PostsRepo
from .vault import VaultRepo


class Store(IdentityRepo, VaultRepo, PostsRepo, LedgerRepo, OutboxRepo, KpiRepo):
    SCHEMAS = (identity.SCHEMA, vault.SCHEMA, posts.SCHEMA, ledger.SCHEMA, outbox.SCHEMA, kpi.SCHEMA)
    COLUMNS = identity.COLUMNS

    def __init__(self, path: str, secret_key: "str | SecretValue"):
        super().__init__(path)
        self.sealer = Sealer(secret_key)

    def purge_expired(self) -> None:
        self.purge_expired_identity()
        self.purge_expired_tickets()
        self.purge_outbox()
        self.purge_kpis()


__all__ = ["Database", "IdentityRepo", "KpiRepo", "LedgerRepo", "OutboxRepo", "PostsRepo", "Sealer", "Store",
           "VaultRepo", "new_secret", "token_hash"]
