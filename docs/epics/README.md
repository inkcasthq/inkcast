# Epics

Each epic has its own folder with four documents. Together they meet the [definition of done](../definition-of-done.md):

| Document | Written | Holds |
|---|---|---|
| `design.md` | before building | use cases, system design and diagrams, data and API sketch, mockups (UI epics), risks |
| `stories.md` | before building; ticked while building | features → stories, acceptance criteria, design note per story |
| `implementation.md` | while building | what was built, file map, configuration, rollout and rollback, verification evidence |
| `review.md` | after building | independent review findings and how each was resolved |

The backlog behind these is code: `docs/backlog/*.yaml`, filed as GitHub issues by `scripts/backlog/file.py`.

| Epic | Goal | Status |
|---|---|---|
| [E1 Platform foundations](E1-foundations/design.md) | One codebase, many services; shared libraries; launcher, prefixes, Gateway; config gate; split-mode CI | Done: reviewed, 0 open P0/P1 |
| [E2 Git publishing](E2-git-publishing/design.md) | Personal access tokens, publish ledger, `stories:sync`, in-place updates, CLI, GitHub Action | Done: reviewed, 0 open P0/P1 |
| [E3 Tenancy, data and authorization](E3-tenancy-authorization/design.md) | PostgreSQL with backups and a restore drill; Valkey; organizations and workspaces; **authorization on OpenFGA from day 1** ([design](../authorization.md)): model as code (done), pluggable engine, one enforcement path, Keycloak sync | Planned (model done) |
| [E4 SaaS web app](E4-web-app/design.md) | React SPA, BFF session, onboarding, connections, composer, calendar, members, approvals, customer and team insights | Mockups for approval |
| [E5 Billing, entitlements and metering](E5-billing/design.md) | Delivers E10: `SubscriptionService`, catalogue as code, entitlements after OpenFGA (403 → 409 → 402), webhooks, offline licences, Agency add-on | Planned |
| E6 Admin portal | Operator API and UI behind Keycloak `inkcast-admin`: users, queues, audit, overrides, owner KPIs (growth, revenue, platform health) | Planned |
| E7 Reliability, observability and operations | Alerts and SLOs as code, dashboards, the four metric layers and KPIs ([design](../design/observability-and-kpis.md), PR #8), channel health, contract tests, notifications, traces, backups and DR, edge protection, cost | Foundations done (PR #8); the rest planned |
| E8 Social platforms | Instagram, Facebook Pages, YouTube, TikTok, X; batch review ([design](../design/social-platforms-epic.md), [video and review](../design/video-platforms-and-review.md)) | Planned |
| [E9 Identity on Keycloak](E9-identity-keycloak/design.md) | SaaS realm as code, self sign-up, social login, MFA, enterprise SSO, user sync, real Keycloak in CI. Needed before E4 and E6 | Next |
| [E10 Tiers and subscriptions (spike)](E10-tiers-and-billing/design.md) | Free, Personal, Team, Business, Enterprise per seat; Agency add-on; provider bake-off; pricing validation | Next |

## Delivery phases

The owner's order of delivery (2026-10-29). Each open feature in `docs/backlog/*.yaml` carries `phase:`, and a story
carries its own only when it differs from its feature's; `tests/test_backlog_phases.py` keeps every open feature in a
phase. A phase starts when the one before it can be used, not when every story in it is done.

| Phase | Goal | What is in it |
|---|---|---|
| 1 | One writer uses Inkcast from a browser, on staging and then production | E4 shell, the `inkcast-web` sign-in and deployment, and the screens from the approved writer app mockup (Posts, Compose, Connections, Git publishing, Tokens, Insights, API) and the calendar. What is in progress: repository hardening (F1.8), the OpenShift Local deployment (F3.9.S5–S7), the SQLite backup stopgap (F3.1.S9), erasure (F3.3.S5), the Keycloak component (F9.1.S3). Beside it, the security bugs from reviews: #112, #139, #149, #161, #212, #214 |
| 2 | Several people share a workspace | E3: PostgreSQL repositories, organizations and workspaces, one enforcement path, relationship lifecycle, OpenFGA operations, Valkey. E4 onboarding, members, approvals, the team view. E9 personal organization and membership sync (F9.2.S2, F9.5.S1, F9.5.S3). Publish now through the queue (F7.5) |
| 3 | Identity and the account lifecycle are complete | E9: social login, MFA, the account-cleanup job, enterprise SSO, LDAP, runbooks. E3 export, legal hold and import (F3.3.S7–S10). E7 notifications (F7.4) and channel health (F7.2) |
| 4 | Inkcast is sold and operated | E10 (the spike decides), then E5 billing and entitlements; E6 admin portal (needs F9.4's MFA) |
| 5 | More platforms | E8, per-workspace platform app credentials (F3.4), engagement (F4.10.S4), media storage (F7.6.S2) |
| ongoing | Beside every phase | E7 alerts, contract tests, dashboards, traces, backups and DR, edge protection, cost, SLOs; the image on Python 3.15 (F1.6.S3); Inkcast on a Mac (F1.9) |

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
