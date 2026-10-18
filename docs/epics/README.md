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
| [E9 Identity on Keycloak](E9-identity-keycloak/design.md) | SaaS realm as code, self sign-up, social login, MFA, enterprise SSO, user sync, real Keycloak in CI. Needed before E4 and E6 | Next |
| E3 Tenancy and data | PostgreSQL repositories, workspaces, members and invites, row-level security, audit log | Planned |
| [E4 SaaS web app](E4-web-app/design.md) | React SPA, BFF session, onboarding, connections, composer, calendar | Mockups for approval |
| E5 Billing | Pluggable `BillingProvider`, plans and entitlements, metering, webhooks | Planned |
| E6 Admin portal | Operator API and UI behind Keycloak `inkcast-admin`: users, queues, audit, overrides | Planned |
| E7 Reliability | Alert catalogue, channel health, capability manifest, contract tests, outbound webhooks | Planned |
| E8 Social platforms | X, TikTok, Instagram ([design](../design/social-platforms-epic.md)) | Planned |

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
