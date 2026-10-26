# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Persistence shared library.

``Store`` is the union of the domain repositories over one connection. Each repository is
owned (written) by one service:

=================  ==================  =============================================
Repository         Owner               Tables
=================  ==================  =============================================
IdentityRepo       IdentityService     users, oauth_clients, pending_auth, auth_codes,
                                       tokens, api_tokens, sync_state, deletion_requests,
                                       sync_breaker_trips, sync_breaker_people
VaultRepo          ConnectorService    credentials, connect_tickets
PostsRepo          PublishingApi       posts, heartbeats (scheduler writes status)
LedgerRepo         PublishingApi       ledger
OutboxRepo         (the row's owner)   outbox, outbox_cursors (events written with the row;
                                       drained by InsightsService)
KpiRepo            InsightsService     kpi_rollups, kpi_applied
AuthzRepo          LocalEngine         authz_tuples (only with INKCAST_AUTHZ_ENGINE=local)
ErasureRepo        IdentityService     user_erasures (erase_user: every table above, one transaction;
                                       the guard that refuses a row for a person without a users row)
=================  ==================  =============================================
"""

from __future__ import annotations

from ..config import RetentionPolicy
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
    INDEXES = identity.INDEXES

    def __init__(self, path: str, secret_key: "str | SecretValue"):
        super().__init__(path)
        self.install_owner_guards()         # no row for a person who has no users row (issue #196); one read once in place
        self.sealer = Sealer(secret_key)
        self._erasure_salt = derive_key(secret_key, "erasure")
        self.backfill_erasure_hashes()      # the rows from before users.erasure_hash (issue #113); then one read per open

    def purge_expired(self, retention: RetentionPolicy = RetentionPolicy()) -> None:
        """Housekeeping: expired grants and tickets, then domain events every consumer has passed and hourly KPI buckets
        (with their ``kpi_applied`` rows) older than ``retention``. The scheduler passes ``Config.retention``
        (INKCAST_OUTBOX_RETENTION_DAYS, INKCAST_KPI_HOURLY_RETENTION_DAYS); the default is the design's 7 and 90 days."""
        self.purge_expired_identity()
        self.purge_expired_tickets()
        self.purge_outbox(retention.outbox_seconds)
        self.purge_kpis(retention.kpi_hourly_seconds)


__all__ = ["AuthzRepo", "Database", "ErasureRepo", "IdentityRepo", "KpiRepo", "LedgerRepo", "OutboxRepo", "PostsRepo",
           "Sealer", "Store", "TupleConflict", "VaultRepo", "new_secret", "token_hash"]
