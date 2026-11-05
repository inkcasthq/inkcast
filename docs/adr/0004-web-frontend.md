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
- `web/package-lock.json` is resolved in CI by `web-lock-refresh.yml`: from scratch, on Linux, with the Node that
  `web/package.json` pins. The first lock PR adds a web CI job that installs it with `npm ci`.
  - Why: a lock written where `node_modules` exists can leave out other platforms' optional native packages
    ([npm/cli#4828](https://github.com/npm/cli/issues/4828)). A Vite lock from an arm64 Mac then fails on Linux with
    "Cannot find module @rollup/rollup-linux-x64-gnu" ([npm/cli#8320](https://github.com/npm/cli/issues/8320)).
  - npm 11.3.0 fixed #4828 ([npm/cli#8184](https://github.com/npm/cli/pull/8184)), but npm 10, which Node 22 still
    ships, did not. One pinned npm on CI's Linux keeps the lock independent of the npm a contributor runs.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
