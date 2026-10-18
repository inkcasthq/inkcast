# Accounts: API tokens issued at sign-up

labels: accounts,security

On first sign-in create a personal workspace and issue an API token, shown once on a one-time page (never through chat).

- format `ink_pat_<random>`, stored as SHA-256, scopes `publish`/`read`, 90-day default expiry, last-used timestamp
- accepted as `Authorization: Bearer` on `/mcp` and the REST API, alongside OAuth tokens
- create / list / revoke via the admin portal and a tool that returns a one-time link

**Done when:** tests prove a revoked or expired token is refused and tokens are never logged.
