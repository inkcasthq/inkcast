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
- **Version mix (Idempotency-Key, #475):** a release from before 321df2a (#466) releases and completes a reservation by user and key alone (`DELETE FROM idempotency WHERE user_id = ? AND key = ? AND response = ''`, `UPDATE idempotency SET response = ? WHERE user_id = ? AND key = ?`). Run beside a later release on one database, it frees or overwrites that release's pending reservations, and the later release can't prevent it. Only two PublishingApi processes of different releases running at once mix them. The shipped manifests run PublishingApi as one replica with `strategy: Recreate`, both the default overlay and `components/split-services`, and for a rollout Kubernetes removes the old Pod before it creates the new one. So a rollout, or a rollback by reverting the digest PR, does not mix them. These do:
  - a Pod force-deleted while it still runs (`kubectl delete pod --force --grace-period=0` frees its name before the kubelet confirms it stopped);
  - the all-in-one `inkcast` Deployment left running beside `components/split-services`' `inkcast-publishing-api`, or the other way round;
  - more than one replica, or `RollingUpdate` (E3's split services on PostgreSQL).

  Across 321df2a, stop the old PublishingApi before the new one starts.

## Verification

- `tests/test_git_publishing.py` covers tokens and scopes, revocation, create → unchanged → update/stale (and that nothing is ever created twice), dry run, idempotency replay, unconfirmed → confirmed → created once, re-scheduling, invalid input, the CLI against a temporary repository (job summary, outputs, raw image URLs), and the browser token flow. It runs in both modes.
- The test count and the CI run are recorded in `review.md`.

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)
