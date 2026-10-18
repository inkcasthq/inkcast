# ADR-0006: Identity on Keycloak, authorization in Inkcast

**Status:** accepted, 2026-10-18; amended 2026-10-18 (authorization moves to OpenFGA, [ADR-0007](0007-openfga-authorization.md)) · **Epic:** E9

## Context
The SaaS needs several things from identity:
- self sign-up, with email verification;
- social login and account linking;
- MFA;
- enterprise single sign-on for customers;
- an operator role for the admin portal.

Every client must use the same identity: the web app, the admin portal, the mobile app, and the browser step when Claude Code connects. MCP clients, meanwhile, need an OAuth server with dynamic client registration, MCP metadata (RFC 9728), per-client consent, and personal access tokens that never pass through an AI.

## Options
1. **Keycloak for everything, including the MCP OAuth server.** One system to run, but consent, PAT scopes and the "no tokens in AI transcripts" rule would live in Keycloak configuration, and the MCP-specific behaviour would be fragile.
2. **Inkcast for everything:** build sign-up, MFA, social login and SSO in-house. Expensive, and risky to get wrong.
3. **Split:** Keycloak answers *who you are*; Inkcast answers *what this client or token may do*.

## Decision
Option 3.

**Keycloak**, one `inkcast` realm per environment, exported to Git, owns:
- registration and email verification;
- social identity providers (Google, GitHub, Microsoft; Apple for mobile later);
- account linking by verified email;
- brute-force detection;
- MFA (passkeys or OTP), required for `inkcast-admin`;
- Organizations, so enterprise domains route to the customer's own IdP;
- the platform-level realm roles `inkcast-user` (default) and `inkcast-admin`.

**Inkcast IdentityService** keeps:
- the OAuth server for MCP clients, delegating the sign-in step to Keycloak over OIDC with PKCE;
- consent pages;
- personal access tokens and their scopes.

Users are keyed `oidc:<issuer>#<sub>` and created on first login. Workspaces, workspace roles, plans and data live in Inkcast (E3, E5). Keycloak admin events (user disabled or deleted) are consumed so access is revoked everywhere.

## Amendment: three owners

| Concern | Owner |
|---|---|
| Authentication (who are you?) | **Keycloak**: login, MFA, OIDC tokens, Organizations |
| Authorization on resources (can you do this to *this* thing?) | **OpenFGA** ([ADR-0007](0007-openfga-authorization.md), [design](../authorization.md)) |
| Business state | **Inkcast DB**: approval status, plan and entitlements, quotas |

The realm roles `inkcast-user` and `inkcast-admin` stay. They are platform-level only: "may use Inkcast at all" and "is a ScalarOps operator". Tenant roles (owner, admin, manager, editor, reviewer, publisher, billing admin, client reviewer) are OpenFGA relations, never realm roles or token claims. Inkcast IdentityService still owns MCP OAuth, consent and personal access tokens. A token's scopes can only narrow what OpenFGA allows.

## Consequences
- **Running Keycloak:** Keycloak is a required backing service for the SaaS. CI runs a real Keycloak with the exported realm.
- **Small installs:** self-hosters without Keycloak can still use any OIDC provider, GitHub, or a password, through the same pluggable `IdentityProvider`.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
