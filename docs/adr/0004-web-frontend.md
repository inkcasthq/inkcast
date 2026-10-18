# ADR-0004: Web front end

**Status:** accepted, 2026-10-18 · **Epic:** E4

## Decision
- A **React + TypeScript** single-page app, built with Vite, in `web/` (npm workspaces):
  - `apps/app`: writers;
  - `apps/admin`: operators, as a separate bundle;
  - `packages/ui`: the design system;
  - `packages/api-client`: generated from the OpenAPI document.
- The **WebApp** service serves the static build and a small **backend-for-frontend**:
  - it signs in through Keycloak (OIDC) and keeps an `HttpOnly`, `SameSite` session cookie;
  - **the browser never holds bearer tokens.**
- **Mockups are approved before any UI code** (definition of done).

## Why
- The calendar, composer and live previews are interactive enough that server-rendered HTML gets awkward.
- React has the largest hiring pool for juniors.
- A BFF keeps tokens out of JavaScript, which removes the main XSS-to-account-takeover path.

## Consequences
- A Node toolchain enters CI, as the `web` job.
- The build sandbox used for development can't reach npm, so the web app is built and tested in GitHub CI.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
