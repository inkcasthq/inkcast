# Data: Postgres, migrations and workspaces

labels: data

Move from SQLite to Postgres (keep SQLite for local single-user).

- migrations tool; `workspaces`, `memberships`; `workspace_id` on posts and credentials
- every query scoped by workspace; isolation tests that try cross-tenant access

**Done when:** test suite runs against both backends.
