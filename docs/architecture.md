# Inkcast architecture (v0.1)

## Components

```
                 ┌──────────────────────── Inkcast server ────────────────────────┐
 MCP client      │  OAuth 2.1 AS  ──►  Identity provider (pluggable)             │      Keycloak / any OIDC
 (Claude Code) ─►│  /register /authorize /token /revoke     oidc | github | password ├──►  GitHub OAuth app
                 │  consent page                                                  │
                 │                                                                │
                 │  MCP resource /mcp  ──►  tools  ──►  Publisher ──► Platform plug-ins ├──►  Medium API
                 │                                         │         (entry points)    ├──►  Forem/DEV API
 Browser ───────►│  /connect/{ticket} (enter platform keys)│                           │
                 │                                         ▼                           │
                 │                 Store (SQLite → Postgres)   ◄── Scheduler loop      │
                 └────────────────────────────────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `auth/provider.py` | OAuth 2.1 authorization server for MCP clients: dynamic client registration (RFC 7591), PKCE S256, single-use codes, rotating refresh tokens, revocation (RFC 7009). Metadata per RFC 8414 / RFC 9728 is served by the MCP SDK. |
| `auth/identity.py` | Who the person is. `OIDCIdentity` (Keycloak or any OpenID Connect provider, with PKCE), `GitHubIdentity`, `PasswordIdentity`. Each returns a stable identity key, `oidc:<issuer>#<sub>` or `github:<id>`. |
| `auth/pages.py` | Sign-in, consent and connect pages, with a strict Content-Security-Policy, no framing, and `no-store` caching. |
| `platforms/` | The `Platform` interface, built-ins (Medium, DEV) and the entry-point registry. |
| `story.py` | Platform-neutral story: front matter, per-platform overrides, image URL fixing, schedule validation. Pure: no I/O. |
| `publisher.py` | Fan-out to platforms, immediate sends, scheduled sends with retry (`60s`, `5m`, `30m`), cancel. |
| `storage.py` | Users, OAuth state, encrypted credentials, posts. |
| `server.py` | MCP tools, browser routes, app assembly, scheduler task. |

## Sign-in flow (what Claude Code does on `/mcp` → Authenticate)

1. `POST /mcp` without a token returns `401` with `WWW-Authenticate: Bearer resource_metadata=…`.
2. The client reads `/.well-known/oauth-protected-resource/mcp`, then `/.well-known/oauth-authorization-server`.
3. It registers itself at `POST /register` (dynamic registration).
4. It opens `/authorize` in the browser with PKCE. Inkcast stores a *pending* request and redirects to `/auth/login`.
5. The identity provider round-trip runs: Keycloak, GitHub or the password form, ending at `/auth/callback`.
6. The callback maps the identity to an Inkcast user (`upsert_user`) and shows the **consent page**, naming the client and its redirect host.
7. On approve, Inkcast issues a single-use code and redirects to the client. The client exchanges it at `/token` with the PKCE verifier.

## Security model

**Credentials and tokens**

- Platform credentials are encrypted with a Fernet key derived from `INKCAST_SECRET_KEY`. Rotating that key requires reconnecting platforms; a KMS-backed envelope is planned (issue 09).
- OAuth codes and access/refresh tokens are stored only as SHA-256 hashes.
- Access tokens last 1 hour and refresh tokens 30 days. A refresh revokes the whole token family.
- Platform keys never pass through the AI. `inkcast_connect` returns a one-time, 15-minute link instead. The key is verified against the platform before it is stored.

**Authorization**

- Consent is required for every authorization. Dynamically registered clients can't obtain tokens silently, which addresses the "confused deputy" risk in the MCP security guidance.
- Every tool resolves the user from the token's subject, and every query is scoped to that user.
- Hosted mode refuses to start unless sign-in is restricted (role, email domain, org or user list), or open sign-up is enabled explicitly.

**Transport and pages**

- DNS-rebinding protection checks `Host` headers on `/mcp`. `INKCAST_PUBLIC_URL` must be `https` unless it is localhost.
- Pages send a strict Content-Security-Policy, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.

## Known limits of v0.1

- **Storage:** single node, SQLite.
- **Scheduler:** one in-process loop. Claiming is atomic, so a second worker is safe, but there is no leader election.
- **Platforms:** Medium cannot edit or delete via its API, and DEV unpublishing needs the platform UI. Inkcast cancels only what hasn't been sent.
- **Images:** they must be public URLs. Uploading to Medium's image endpoint is planned.
