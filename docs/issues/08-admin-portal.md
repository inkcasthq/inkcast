# Admin portal (Keycloak-authenticated)

labels: portal

Separate web app (SPA + BFF) signed in with Keycloak OIDC (Authorization Code + PKCE), restricted to `inkcast-admin` for platform admins and to workspace owners for their workspace.

- users (from Inkcast's user table, keyed by Keycloak `sub`), workspaces, memberships, roles
- API tokens: issue, revoke; connected platforms: metadata only, never secrets
- audit log viewer; usage and billing hooks

**Done when:** portal never reads decrypted platform credentials; all actions audited.
