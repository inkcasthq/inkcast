# ADR-0002: The gateway

**Status:** accepted, 2026-10-18 · **Epic:** E1

## Context
Clients (Claude Code, the GitHub Action, browsers, the future web app) must see stable public paths (`/mcp`, `/api/v1/...`, `/authorize`, `/connect/...`) whatever the service split behind them. Clusters vary: some have a Gateway API implementation, OpenShift has Routes, and laptops have nothing.

## Options
1. **Off-the-shelf gateway everywhere** (Kong, APISIX): powerful, but another component to run locally and on-prem.
2. **Kubernetes Gateway API only**: CNCF standard, but nothing for local or single-node.
3. **Both**: Gateway API HTTPRoutes in clusters, plus a small built-in `Gateway` service from the same image for everywhere else.

## Decision
Option 3.
- One **route table**, `libs/contracts/routes.py` (path prefix → service), drives three things:
  - the built-in Gateway;
  - the generated `components/gateway-api` HTTPRoutes;
  - each service's `public_paths`.

  A test fails if they disagree.
- The built-in Gateway:
  - passes `Host` through (services pin it to `INKCAST_PUBLIC_URL` against DNS rebinding);
  - sets `X-Forwarded-*`;
  - streams responses;
  - holds no state.

## Consequences
- Moving a path to another service is a one-line change to the table, plus `scripts/gen-manifests.py`.
- Cross-cutting edge features (rate limiting, WAF) belong in the cluster gateway. The built-in one stays minimal on purpose.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
