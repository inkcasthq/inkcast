# Epics

Each epic has its own folder with four documents. Together they meet the [definition of done](../definition-of-done.md):

| Document | Written | Holds |
|---|---|---|
| `design.md` | before building | use cases, system design and diagrams, data and API sketch, mockups (UI epics), risks |
| `stories.md` | before building; ticked while building | features → stories, acceptance criteria, design note per story |
| `implementation.md` | while building | what was built, file map, configuration, rollout and rollback, verification evidence |
| `review.md` | after building | independent review findings and how each was resolved |

The backlog behind these is code: `docs/backlog/*.yaml`, filed as GitHub issues by `scripts/backlog/file.py`.

The [backlog tracker](../backlog/TRACKER.md) lists every story with its status, its issue and its place in the execution order. The order is code too: `docs/backlog/order.yaml`, checked and rendered by `scripts/backlog/tracker.py`. Run `scripts/backlog/tracker.py --drift inkcasthq/inkcast` to compare both with the GitHub issues.

| Epic | Goal | Status |
|---|---|---|
| [E1 Platform foundations](E1-foundations/design.md) | One codebase, many services; shared libraries; launcher, prefixes, Gateway; config gate; split-mode CI | Done: reviewed, 0 open P0/P1 |
| [E2 Git publishing](E2-git-publishing/design.md) | Personal access tokens, publish ledger, `stories:sync`, in-place updates, CLI, GitHub Action | Done: reviewed, 0 open P0/P1 |
| [E3 Tenancy, data and authorization](E3-tenancy-authorization/design.md) | PostgreSQL with backups and a restore drill; Valkey; organizations and workspaces; **authorization on OpenFGA from day 1** ([design](../authorization.md)): model as code (done), pluggable engine, one enforcement path, Keycloak sync | In progress: the model, the policy engine and OpenFGA on OpenShift Local are done ([tracker](../backlog/TRACKER.md)) |
| [E4 SaaS web app](E4-web-app/design.md) | React SPA, BFF session, onboarding, connections, composer, calendar, members, approvals, customer and team insights | Mockups approved 2026-10-28; the writer-app mockup (F4.1.S4) is next ([tracker](../backlog/TRACKER.md)) |
| [E5 Billing, entitlements and metering](E5-billing/design.md) | Delivers E10: `SubscriptionService`, catalogue as code, entitlements after OpenFGA (403 → 409 → 402), webhooks, offline licences, Agency add-on | Planned |
| E6 Admin portal | Operator API and UI behind Keycloak `inkcast-admin`: users, queues, audit, overrides, owner KPIs (growth, revenue, platform health) | Planned |
| E7 Reliability, observability and operations | Alerts and SLOs as code, dashboards, the four metric layers and KPIs ([design](../design/observability-and-kpis.md), PR #8), channel health, contract tests, notifications, traces, backups and DR, edge protection, cost | Foundations done (PR #8); the rest planned |
| E8 Social platforms | Instagram, Facebook Pages, YouTube, TikTok, X; batch review ([design](../design/social-platforms-epic.md), [video and review](../design/video-platforms-and-review.md)) | Planned |
| [E9 Identity on Keycloak](E9-identity-keycloak/design.md) | SaaS realm as code, self sign-up, social login, MFA, enterprise SSO, user sync, real Keycloak in CI. Needed before E4 and E6 | In progress ([tracker](../backlog/TRACKER.md)) |
| [E10 Tiers and subscriptions (spike)](E10-tiers-and-billing/design.md) | Free, Personal, Team, Business, Enterprise per seat; Agency add-on; provider bake-off; pricing validation | Next |

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
