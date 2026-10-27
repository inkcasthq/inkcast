# E2 Git publishing: implementation

## What was built

| Area | Files |
|---|---|
| Tokens | `libs/db/identity.py` (`api_tokens`: create, find, list, revoke), `libs/auth/__init__.py` (`TokenVerifier`, `Principal`, `SCOPES`), `services/identity/__init__.py` (REST endpoints, `/tokens/new` browser flow), `libs/http/pages.py` (confirm and reveal pages) |
| Scopes in REST and MCP | `libs/http/rest.py` (`Authed`, problem+json), `services/publishing/rest.py`, `services/publishing/mcp_tools.py` |
| Ledger and sync | `libs/db/ledger.py` (entries, claims, idempotency), `services/publishing/sync.py` (`GitSync`) |
| Platform contract | `libs/platforms/base.py` (`capabilities`, `update`, `find_existing`), `libs/platforms/devto.py` (PUT, list mine), `libs/pipeline.py` (`update`, `confirm`, remote ids on outcomes) |
| Content | `libs/content/story.py`: `schedule_as_written` for files re-read after their date, kept as scheduled posts at the time written ([ADR-0015](../../adr/0015-publish-at-is-always-scheduled.md)) |
| Contracts | `libs/contracts/models.py` (Token*, Sync*, LedgerEntry), `libs/contracts/openapi.py` (new operations and the scope table) |
| CLI and Action | `cli.py` (`inkcast sync`), `action/action.yml`, `docs/examples/publish-on-push.yml` |
| Tests | `tests/test_git_publishing.py` (single process and split), `tests/fakes.py` (DEV `PUT` and `GET /articles/me/all`) |

## Data

New tables, created on start-up and additive: `api_tokens`, `ledger`, `idempotency`. No change to existing tables.

## Configuration

None new on the server. In CI, `INKCAST_URL` and `INKCAST_TOKEN` are read by `inkcast sync`.

## Rollout and rollback

- **Rollout:** deploy. Then create a token (`inkcast_create_token` in Claude Code, or `POST /api/v1/tokens`) and add the example workflow to a blog repository.
- **Rollback:** the previous digest ignores the new tables. Any token created meanwhile stops working until you roll forward again.

## Verification

- `tests/test_git_publishing.py` covers tokens and scopes, revocation, create → unchanged → update/stale (and that nothing is ever created twice), dry run, idempotency replay, unconfirmed → confirmed → created once, re-scheduling, invalid input, the CLI against a temporary repository (job summary, outputs, raw image URLs), and the browser token flow. It runs in both modes.
- The test count and the CI run are recorded in `review.md`.

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)
