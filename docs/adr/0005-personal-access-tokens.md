# ADR-0005: Personal access tokens

**Status:** accepted, 2026-10-18 · **Epic:** E2

## Context
CI (the GitHub Action, `inkcast sync`) can't do an interactive OAuth sign-in. It needs a long-lived credential that can do *only* what CI needs, and that can be revoked without signing everyone out.

## Decision
- **Format:** `ink_pat_<12-hex id>_<43-char secret>`. The id is shown in lists and logs (the Redactor keeps it and masks the rest); the secret never is.
- **Storage:** SHA-256 hash only. The plain token is returned **once**, either by `POST /api/v1/tokens` (to a program) or on the `/tokens/new` page (to a person, after a fresh sign-in and an explicit confirmation).
- **Scopes:** `posts:read`, `posts:write`, `stories:sync`, `connections:read`, `connections:write`. The OAuth scope `inkcast` implies them all. Every route and MCP tool checks one.
- **A PAT can't manage tokens.** Token management requires an OAuth session, so a leaked CI token can't mint more tokens or hide itself.
- **Not through the AI:** the MCP tool `inkcast_create_token` returns a link to `/tokens/new`, never a token. Tokens never enter an AI transcript.
- **Expiry:** settings since F1.10.S2 (issue #90), with the original policy as their defaults: `INKCAST_PAT_DEFAULT_DAYS` (90) when the request doesn't say, at most `INKCAST_PAT_MAX_DAYS` (366, the absolute ceiling, which the API document keeps as `TokenCreate`'s), or never (REST `expires_in_days: null`, `/tokens/new?days=never`) only while `INKCAST_PAT_ALLOW_NO_EXPIRY` is true (the default; a posture `info` in production). Every entry point (REST, `/tokens/new`, the MCP tool) enforces the limit where the token is made, with a 422 (a tool error) naming it. A lower maximum doesn't shorten tokens already issued: they keep the expiry they were given. The last-used time is recorded at most once a minute.

## Consequences
- **Revocation** is immediate (the lookup checks `revoked_at`).
- **Workspace scoping** arrives in E3: tokens will belong to a workspace and carry its id.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
