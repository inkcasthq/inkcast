# Inkcast — product spec

## One line

Inkcast lets writers and developer-relations teams publish one markdown story to every platform they write on, from their AI assistant, with correct formatting and canonical links. It runs as a security-first SaaS or self-hosted.

## The problem

Cross-posting a technical article today means a lot of manual work for every platform:

- reformatting the text
- re-entering the title, subtitle and tags
- remembering the platform's limits
- setting the canonical link back to your blog

Existing social schedulers treat long-form platforms as an afterthought:

- **They lose fields:** subtitles are dropped.
- **They ignore platform rules:** tag limits and title handling vary per platform.
- **They're built for clicking, not AI agents.**

## Who it's for

1. **Technical writers and engineers** who blog on their own site and syndicate to Medium, DEV and Hashnode.
2. **DevRel and content teams** publishing under company publications and organizations, with review and drafts.
3. **Later: agencies** managing several brands, i.e. several workspaces.

## Product pillars

1. **AI-native.** MCP is the primary interface (Claude Code, Claude, any MCP client), with REST for everything else.
2. **Platform-correct.** Each platform plug-in encodes that platform's real rules. A dry run shows exactly what will be sent.
3. **Pluggable.** New platforms and identity providers are plug-ins behind stable interfaces.
4. **Security first.** Secrets never pass through the AI, everything is encrypted or hashed at rest, consent is explicit, and all data is tenant-scoped.

## Identity and accounts (target design)

- **Keycloak is the only identity system.** It serves both the product and the admin portal, and it brokers social login: Google, GitHub, Microsoft, Apple, GitLab, LinkedIn.
  - Medium is not an option: it no longer offers sign-in or API access to new apps.
- **Keycloak links a person's accounts.** Through first-broker-login and account linking on verified email, a person who signs up with Google and later uses GitHub stays one user. Inkcast stores users by Keycloak's stable `sub` and never merges identities itself.
- **Sign-up creates a workspace and issues an API token,** shown once. Tokens are:
  - prefixed, e.g. `ink_pat_…`, and stored hashed
  - scoped, e.g. `publish` or `read`
  - set to expire, and revocable from the portal

  A token works as `claude mcp add … --header "Authorization: Bearer ink_pat_…"` and for REST/CI. OAuth (`/mcp` → Authenticate) remains the default for interactive clients.
- **Roles per workspace:** owner, admin, member. Keycloak realm roles control platform-level access (e.g. `inkcast-admin` for the admin portal).
- **The admin portal** is a separate web app using Keycloak OIDC (Authorization Code + PKCE). It covers:
  - users and workspaces
  - token issuance and revocation
  - connected platforms (metadata only, never secrets)
  - audit log, usage and billing hooks
- **The mobile app (later)** is a public OIDC client against the same Keycloak (PKCE), plus the same REST API. Sign-up happens through Keycloak.

## Architecture (target)

| Component | Notes |
|---|---|
| Inkcast API + MCP server | This repo. Stateless web tier. |
| Worker | The scheduler as its own process, using row-level claiming in Postgres. |
| Postgres | Tenancy columns on every table, plus migrations. |
| Secrets | Envelope encryption with a cloud KMS or Vault, per-tenant data keys and rotation. |
| Object storage | Uploaded images and cover images. |
| Keycloak | One realm per environment, social identity providers, admin portal client, product client, mobile client. |
| Admin portal | Separate SPA/BFF behind Keycloak. |

## Security requirements

- **Secrets and tokens**
  - OAuth 2.1 for MCP: dynamic registration, PKCE S256, consent, single-use codes, rotating refresh tokens.
  - Platform credentials go only through one-time browser pages, never through chat or AI context.
  - Every secret is hashed or encrypted at rest. Plaintext is never logged.
- **Access and accountability**
  - Tenant isolation is enforced in the data layer and in tests.
  - An audit log records sign-ins, token issuance, connect and disconnect, publish and cancel.
- **Abuse and supply chain**
  - Rate limits are applied per token and per workspace.
  - Outbound requests are allow-listed per platform plug-in.
  - Dependencies are pinned, and CI runs tests, SAST and an image scan.

## Phases

| Phase | Scope |
|---|---|
| **0 — done (v0.1)** | MCP server; Medium and DEV plug-ins; scheduler; OIDC/GitHub/password sign-in; encrypted credentials; consent page; end-to-end tests. |
| **1 — hosted beta** | Keycloak realm and compose dev stack; Postgres; workspaces; API tokens at sign-up; separate worker; KMS envelope encryption; Hashnode, WordPress and Ghost plug-ins; notifications. |
| **2 — SaaS** | Admin portal; billing; audit log UI; usage limits; image upload; short-form announcements (Bluesky, Mastodon, LinkedIn, X) linking to the published story. |
| **3 — mobile** | Sign-up, connect, review drafts, approve and schedule from the phone. |

## Non-goals (for now)

- A WYSIWYG editor. Markdown in your repo stays the source of truth.
- Scraping, or automating platform UIs that don't offer an API.

---

## Refined prompt (for the next build step)

> Build Phase 1 of Inkcast on top of the existing repo:
> 1. Add a `docker compose` dev stack containing Keycloak, Postgres and Inkcast. Include a realm import with:
>    - a confidential `inkcast` client (PKCE S256, redirect `/auth/callback`)
>    - an `inkcast-admin` client for the admin portal
>    - realm roles `inkcast-user` (a default role) and `inkcast-admin`
>    - placeholder Google and GitHub identity providers
>    - first-broker-login configured to link accounts by verified email
> 2. Move storage to Postgres with migrations. Add `workspaces` and `memberships`, and scope every query by workspace.
> 3. On first sign-in, create a personal workspace and issue an API token, shown once on a one-time page:
>    - prefix `ink_pat_`, stored as a SHA-256 hash, scopes `publish`/`read`, 90-day expiry
>    - accepted as a Bearer token on `/mcp` and on a new REST API
> 4. Split the scheduler into its own worker process, using `SELECT … FOR UPDATE SKIP LOCKED`.
> 5. Replace the local Fernet key with envelope encryption: a KMS interface, a local-dev implementation and a key-rotation command.
> 6. Add Hashnode, WordPress (application passwords) and Ghost (Admin API) plug-ins, each with a fake API and tests.
>
> Keep these from v0.1: secrets never in chat, consent on every authorization, hashed tokens, end-to-end tests with fakes for every external service.
