# Identity: Keycloak dev stack and production realm

labels: identity

`docker compose` with Keycloak, Postgres and Inkcast for local development, plus a documented production realm.

- realm import: `inkcast` client (confidential, PKCE S256, redirect `/auth/callback`), `inkcast-admin` client, roles `inkcast-user` (default role) and `inkcast-admin`
- identity providers: Google, GitHub (disabled placeholders); first-broker-login links accounts by **verified** email only
- realm roles mapper "add to userinfo" on (Inkcast also reads roles from the access token)
- note: Medium cannot be an identity provider (no sign-in for new apps)

**Done when:** `docker compose up` → `claude mcp add …` → sign in through Keycloak works end to end.
