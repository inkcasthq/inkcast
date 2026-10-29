# E3 Tenancy, data and authorization (OpenFGA): design

## Goal

Move Inkcast from one user per account to **organizations and workspaces** on **PostgreSQL**, with authorization on **OpenFGA from day 1**:
- deny by default;
- one enforcement path for web, mobile, REST, MCP and background jobs;
- business state in the DB.

The full authorization design is [docs/authorization.md](../../authorization.md); this page is the epic's view of it.

## What changed in this refinement

E3 originally planned role columns on a members table and a rule that you can only grant roles at or below your own. Both are **replaced**:
- **roles are OpenFGA relations** ([authz/model.fga](../../../authz/model.fga));
- **"who may manage members" is a model rule** (`can_manage_members`).

Isolation keeps database row-level security as defence in depth behind OpenFGA.

| Story | Change |
|---|---|
| F3.2.S1 organizations and workspaces | Refined: business state only; organization id = Keycloak Organization id; a personal organization per account; `kind` and `billing_parent` for agencies |
| F3.2.S2 roles | Replaces the role columns: OpenFGA relations |
| F3.2.S3 managing members | Replaces "at or below your own": `can_manage_members` |
| F3.2.S6 grant API (new) | Writes `seat_assignments` and `agency_grants` with the tuple; checks entitlements first (E5) |
| F3.3 isolation and audit | Refined: RLS behind OpenFGA; cross-tenant tests at model and API level; every grant and revoke audited |
| F3.0, F3.5–F3.9 (new) | Spike, model as code, pluggable engine, one enforcement path, relationship lifecycle, OpenFGA operations |

Full list: [backlog](../../backlog/E3-tenancy-data-and-authorization.yaml).

## System design

<img src="../../diagrams/who-answers-what/who-answers-what.light.png" alt="Who answers what: Keycloak answers who you are (login, MFA, organizations); OpenFGA answers whether you may do this to this object (relations, written by the Inkcast API and synced from Keycloak); the Inkcast database holds business state such as approval status, plan and quotas.">

*Three systems, three questions. No permission ever travels in a token.*

<img src="../../diagrams/check-path/check-path.light.png" alt="One check path: scope, then OpenFGA, then business state, then the plan; 403, 409 and 402 for the first refusal, 503 when authorization errors.">

*Every route, MCP tool and job uses the same `require()`.*

### The model, done in this refinement

`authz/model.fga` and `authz/model.fga.yaml` are committed, and CI runs `fga model test`. Locally the suite reports 15 tests, 50 checks and 4 list-objects checks, all passing.

The owner's model, with three refinements:
- **Removed authors can't edit.** An author keeps edit rights only while they still have workspace access.
- **Publishing needs workspace access.** A direct publisher grant isn't enough on its own.
- **Custom roles stay inside their organization** (review, 2026-10-18). Role grants use their own relations (`role_editor` and the like), which count only for members of the target's organization. A role from another organization, wrongly written onto a workspace or channel, grants nothing; a model test proves it. The member API also refuses such writes (F3.8.S2), because OpenFGA can't compare `role.org` with `workspace.org` itself.

It also adds the team and tier relations (billing admin, client reviewer, member management, usage and audit visibility) and Enterprise custom roles.

### Data

- **Business tables:** organizations and workspaces (Postgres) hold business state only.
- **OpenFGA store:** a separate Postgres database run by the same operator.
- **Outbox:** tuples are written through a transactional outbox, so the database and OpenFGA can't diverge. A reconciler catches drift.

### Organizations, teams and joining

Added 2026-10-28 from the owner's brief: a person sets up a personal organization, a company organization or both, and joins other organizations by invitation, or other people join theirs.

- **Two levels.** An organization owns and pays. Its workspaces hold channels, posts and members. The screens call a workspace a *team*, and the API keeps the name `workspace`. Data belongs to the organization, so a member who leaves takes nothing with them (F3.2.S1).
- **Many memberships.** A person belongs to any number of organizations, and works in one active organization at a time (F3.2.S7). The last owner cannot leave.
- **A personal organization grows in place.** Its first invitation offers to turn it into a team organization: the name and `kind` change, and nothing moves (F3.2.S8). A workspace can move between two organizations that the same person owns (F3.2.S9).
- **Three ways in.**
  - An invitation by email, with a role and an expiry (F3.2.S4).
  - A verified domain, proved by a DNS TXT record and checked again daily, with one mode per domain (F3.2.S10). The default mode is a suggestion that an admin approves. The other modes are an automatic invitation and manual invitation only. A free mail provider's domain is never accepted.
  - Enterprise SSO through the organization's identity provider (F9.6.S1).
- **The type is derived, never typed.** An organization's type follows from facts (F3.2.S11):
  - personal: made at sign-up.
  - team: made or converted by a person, with no verified domain.
  - company: has a verified domain.
  - enterprise: has SSO connected.
  - client: managed by an agency.

  Owners' own labels are stored apart, and nothing reads them for access or billing.
- **No account takeover.** Accounts are never linked by email. A company sign-in reaches an existing account only after the person proves it (F9.3.S2, ADR-0018 section 8).
- **Keycloak's invitation API is not used.** Invitations are Inkcast rows, because CVE-2026-16072 lets an invite link listed by Keycloak's organization API create a member without email verification.

Sources:

- The organization and workspace split: [WorkOS](https://workos.com/docs/authkit/modeling-your-app).
- The join modes: [Clerk](https://clerk.com/docs/guides/organizations/add-members/verified-domains).
- Domain claims: [Slack](https://slack.com/help/articles/5513043606547-Claim-and-verify-email-domains) and [Atlassian](https://support.atlassian.com/user-management/docs/verify-a-domain-to-manage-accounts/).
- Personal accounts turned into teams: [Vercel](https://vercel.com/changelog/2024-01-account-changes).
- Domains unique per realm: [Keycloak organizations](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.0/html/server_administration_guide/managing_organizations).
- The invitation flaw: [Red Hat Bugzilla 2501721](https://bugzilla.redhat.com/show_bug.cgi?id=2501721).

## Risks

- **Check latency on hot paths.** The F3.0.S1 spike measures it, and batching plus `ListObjects` on workspaces mitigate it.
- **Drift between Keycloak, the database and OpenFGA.** Outbox, reconcilers and alerts (E7.F7.1.S2) cover it.

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)
