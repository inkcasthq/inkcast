# Architecture decision records

Each ADR records one significant decision: the context, the options considered, what was chosen and its consequences. Add one when a choice would be expensive to reverse, or when someone will later ask "why is it like this?". Copy the format of an existing ADR; number them in order; never rewrite an accepted ADR — supersede it with a new one.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-one-codebase-many-services.md) | One codebase and one image; services selected by `INKCAST_APP_NAME` | Accepted |
| [0002](0002-gateway.md) | Gateway API in clusters, a built-in Gateway everywhere else | Accepted |
| [0003](0003-data-store.md) | SQLite now with per-domain repositories; PostgreSQL in E3 | Accepted |
| [0004](0004-web-frontend.md) | React + TypeScript SPA with a backend-for-frontend session | Accepted |
| [0005](0005-personal-access-tokens.md) | Personal access tokens: scoped, hashed, shown once, can't mint tokens | Accepted |
| [0006](0006-identity-on-keycloak.md) | Identity on Keycloak, authorization (MCP OAuth, consent, PATs) in Inkcast | Accepted |
| [0007](0007-openfga-authorization.md) | OpenFGA for authorization, pluggable engine, one enforcement path | Accepted |
| [0008](0008-keycloak-openfga-sync.md) | Keycloak → OpenFGA: polled events plus scheduled reconcile | Proposed (spike E3.F3.0.S2) |
| [0009](0009-authorization-identifiers.md) | Tuple ids: Inkcast user id, Keycloak Organization id | Accepted |
| [0010](0010-pricing-and-packaging.md) | Five per-seat tiers, fair seats, Agency add-on, organization is the billing account | Accepted (prices to validate) |
| [0011](0011-entitlements-and-quotas.md) | Plans as code, per-org snapshots, 403 → 409 → 402, atomic quotas | Accepted |
| [0012](0012-offline-licences.md) | Ed25519-signed offline licences; self-host editions | Accepted |
| [0013](0013-billing-webhooks-and-events.md) | Hosted checkout, signed webhooks, inbox/outbox, provider API as truth | Accepted |

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
