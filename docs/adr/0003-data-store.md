# ADR-0003: Data store

**Status:** accepted, 2026-10-18 · **Epic:** E1 (now), E3 (PostgreSQL)

## Context
v0.1 stored everything in one SQLite file. Services need clear data ownership now, and multi-tenant SaaS needs PostgreSQL (workspaces, row-level security, many writers) soon. Neither must block the restructure.

## Decision
- Keep **SQLite (WAL)** for now, behind `libs/db`, split into **domain repositories**, each owned (written) by one service:

  | Repository | Owner | Tables |
  |---|---|---|
  | `IdentityRepo` | IdentityService | users, oauth_clients, pending_auth, auth_codes, tokens, api_tokens |
  | `VaultRepo` | ConnectorService | credentials, connect_tickets |
  | `PostsRepo` | PublishingApi (status: SchedulerWorker) | posts, heartbeats |
  | `LedgerRepo` | PublishingApi | ledger, idempotency |

- In split mode on Kubernetes, the services share the SQLite volume. The pods are pinned to one node by pod affinity, which the generator adds.
- **E3** adds a PostgreSQL implementation of the same repositories:
  - one schema per service;
  - versioned migrations;
  - RLS on `workspace_id`;
  - `FOR UPDATE SKIP LOCKED` for the posts queue.

  SQLite stays for `All` and local use.

## Consequences
- Production split deployments should wait for E3. Until then, split mode is for CI and evaluation.
- Every query takes the caller's user id today, and will take `workspace_id` in E3. Code reviews check this (definition of done #4).

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
