# Architecture decision records

Each ADR records one significant decision: the context, the options considered, what was chosen and its consequences. Add one when a choice would be expensive to reverse, or when someone will later ask "why is it like this?". Copy the format of an existing ADR; number them in order; never rewrite an accepted ADR — supersede it with a new one.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-one-codebase-many-services.md) | One codebase and one image; services selected by `INKCAST_APP_NAME` | Accepted |
| [0002](0002-gateway.md) | Gateway API in clusters, a built-in Gateway everywhere else | Accepted |
| [0003](0003-data-store.md) | SQLite now with per-domain repositories; PostgreSQL in E3 | Accepted |
| [0004](0004-web-frontend.md) | React + TypeScript SPA with a backend-for-frontend session | Accepted |
| [0005](0005-personal-access-tokens.md) | Personal access tokens: scoped, hashed, shown once, can't mint tokens | Accepted |

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
