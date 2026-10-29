# E4 SaaS web app: design (draft for approval)

**Status:** the writer app mockup is approved (2026-10-29); the other screens' mockups are for review. No UI code is written for a screen until its mockup is approved (definition of done, epic #1).

## Use cases

| Screen | The writer can… | Stories |
|---|---|---|
| Onboarding | sign up through Keycloak (Google, GitHub, email), get a workspace, connect a first platform, get a CI token, see the Git guide | F4.3 |
| Connections | see each platform's health and what it can do; reconnect *before* scheduled posts fail | F4.4 (health from E7) |
| Composer | write once and see exactly what each platform will receive, with counters and warnings, then schedule in their own time zone | F4.5 |
| Calendar | see the week or month, drag scheduled posts, and spot failed or unconfirmed posts | F4.6 |
| Git publishing | see what each repository file became on each platform (the ledger), including stale and unconfirmed results | F4.7 / E2 |
| Tokens | create, see and revoke personal access tokens (each shown once) | F4.7 / E2 |

## Mockups

Self-contained HTML (open in a browser) plus PNG. Light theme shown; the design system adds dark mode. All screens work down to 360 px wide (`composer-mobile.png`).

| | |
|---|---|
| [Onboarding](mockups/onboarding.html) ![](mockups/onboarding.png) | [Connections](mockups/connections.html) ![](mockups/connections.png) |
| [Composer](mockups/composer.html) ![](mockups/composer.png) | [Calendar](mockups/calendar.html) ![](mockups/calendar.png) |
| [Git publishing](mockups/git-publishing.html) ![](mockups/git-publishing.png) | [Tokens](mockups/tokens.html) ![](mockups/tokens.png) |
| [Members and seats](mockups/members-seats.html) (F4.8) ![](mockups/members-seats.png) | [Approvals](mockups/approvals.html) (F4.9) ![](mockups/approvals.png) |

Members and approvals (added 2026-10-18) follow the [authorization design](../../authorization.md): roles are OpenFGA relations, seats follow them ([E10](../E10-tiers-and-billing/design.md)), and approval status lives in the Inkcast database. Billing, usage, pricing and paywall screens are in [E10's mockups](../E10-tiers-and-billing/mockups/).

### The writer app (approved 2026-10-29)

One app with a tab per screen, built to the [mockup standard](../../tooling/mockup-standard.md) (F4.1.S4, issue #347) and approved by the owner on 2026-10-29. The screen builds translate from these tabs: Posts F4.6.S3, Compose F4.5.S2, Connections F4.4.S2, Git publishing F4.7.S3, Tokens F4.7.S2, Insights F4.10.S2, API F4.7.S4. The canvas is in [`mockups/writer-app/source/`](mockups/writer-app/source/) (`Main.dc.html` is the app; `Colors.dc.html` the theme tokens); the pages below are exported from it by `scripts/mockups/check.py --export`, in light theme with sample data.

| Field | Value |
|---|---|
| `framework` | `react-ts` (ADR-0004) |
| `styling` | CSS custom properties from `tokens.css`, generated from the canvas's two token tables |
| `state` | `tab`, the period (`range`), the post filter (`show`) and the ledger `source` in the URL; the theme (Light, Dark, Auto) and the unsent draft in browser storage; a new token's value in memory only (shown once) |
| `data` | `/api/v1/me`, `/api/v1/platforms` (and `connect-link`), `/api/v1/posts`, `/api/v1/preview`, `/api/v1/ledger`, `/api/v1/tokens`, `/api/v1/insights/summary` and `/kpis`, `/api/openapi.json`. The scope list has no endpoint yet (`SCOPES` in `libs/auth`; F4.7.S4) |

| | |
|---|---|
| [Posts](mockups/writer-app/posts.html) ![](mockups/writer-app/posts.png) | [Compose](mockups/writer-app/compose.html) ![](mockups/writer-app/compose.png) |
| [Connections](mockups/writer-app/connections.html) ![](mockups/writer-app/connections.png) | [Git publishing](mockups/writer-app/sync.html) ![](mockups/writer-app/sync.png) |
| [Tokens](mockups/writer-app/tokens.html) ![](mockups/writer-app/tokens.png) | [Insights](mockups/writer-app/insights.html) ![](mockups/writer-app/insights.png) |
| [API](mockups/writer-app/api.html) ![](mockups/writer-app/api.png) | Posts at 390 px ![](mockups/writer-app/posts-mobile.png) |

## System design

- **WebApp service.** It serves the built SPA and a backend-for-frontend (ADR-0004). The BFF signs in with Keycloak, keeps an `HttpOnly`, `SameSite=Lax`, `Secure` session cookie, and calls the other services with the user's identity. The browser never holds a bearer token. It is named `inkcast-web` and runs in each environment's own namespace, on a host of its own (`inkcast-web-<side>`, beside the API's `inkcast-<side>`) with its own Keycloak client `inkcast-web` (F4.2.S1, F4.2.S3).
- **Previews come from the server.** The composer calls `POST /api/v1/preview`, which runs the same `prepare()` as publishing, so the preview can't drift from what is sent.
- **Times.** Times are stored in UTC with the IANA zone the writer picked, and expanded in that zone (lessons: integer offsets break across daylight saving).
- **Health.** Health badges and "posts at risk" come from E7's channel-health state machine.

## Decisions to confirm in review

1. The navigation and information architecture in the sidebar.
2. The composer layout: a split preview by default, or tabs per platform.
3. Whether the calendar opens on the week view or the month view.

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)
