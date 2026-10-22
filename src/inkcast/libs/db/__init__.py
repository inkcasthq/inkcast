# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Persistence shared library.

``Store`` is the union of the domain repositories over one connection. Each repository is
owned (written) by one service:

=================  ==================  =============================================
Repository         Owner               Tables
=================  ==================  =============================================
IdentityRepo       IdentityService     users, oauth_clients, pending_auth, auth_codes,
                                       tokens, api_tokens, sync_state, deletion_requests
VaultRepo          ConnectorService    credentials, connect_tickets
PostsRepo          PublishingApi       posts, heartbeats (scheduler writes status)
LedgerRepo         PublishingApi       ledger
OutboxRepo         (the row's owner)   outbox, outbox_cursors (events written with the row;
                                       drained by InsightsService)
KpiRepo            InsightsService     kpi_rollups, kpi_applied
AuthzRepo          LocalEngine         authz_tuples (only with INKCAST_AUTHZ_ENGINE=local)
ErasureRepo        IdentityService     user_erasures (erase_user: every table above, one transaction)
=================  ==================  =============================================
"""

from __future__ import annotations

from ..secrets import Sealer, SecretValue, derive_key, new_secret, token_hash
from . import authz, erasure, identity, kpi, ledger, outbox, posts, vault
from .authz import AuthzRepo, TupleConflict
from .database import Database
from .erasure import ErasureRepo
from .identity import IdentityRepo
from .kpi import KpiRepo
from .ledger import LedgerRepo
from .outbox import OutboxRepo
from .posts import PostsRepo
from .vault import VaultRepo


class Store(IdentityRepo, VaultRepo, PostsRepo, LedgerRepo, OutboxRepo, KpiRepo, AuthzRepo, ErasureRepo):
    SCHEMAS = (identity.SCHEMA, vault.SCHEMA, posts.SCHEMA, ledger.SCHEMA, outbox.SCHEMA, kpi.SCHEMA, authz.SCHEMA,
               erasure.SCHEMA)
    COLUMNS = identity.COLUMNS

    def __init__(self, path: str, secret_key: "str | SecretValue"):
        super().__init__(path)
        self.sealer = Sealer(secret_key)
        self._erasure_salt = derive_key(secret_key, "erasure")

    def purge_expired(self) -> None:
        self.purge_expired_identity()
        self.purge_expired_tickets()
        self.purge_outbox()
        self.purge_kpis()


__all__ = ["AuthzRepo", "Database", "ErasureRepo", "IdentityRepo", "KpiRepo", "LedgerRepo", "OutboxRepo", "PostsRepo",
           "Sealer", "Store", "TupleConflict", "VaultRepo", "new_secret", "token_hash"]
