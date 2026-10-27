# E2 Git publishing: stories

Status key: ✅ done in this epic · ⏭ moved to a later epic (reason given).

## F2.1 Personal access tokens
- ✅ **S1 Format, scopes, storage.** `ink_pat_<id>_<secret>`, stored as a SHA-256 hash, with scopes, expiry and last-used time. *Acceptance:* the token is returned once; lists never show it; a tampered token gets 401 (`test_tokens_and_scopes`, `test_revoked_and_expired_tokens_stop_working`).
- ✅ **S2 Create, list, revoke.** REST, OAuth principals only (a PAT gets 403). The MCP tool `inkcast_create_token` returns a `/tokens/new` link, which leads to a fresh sign-in and a confirmation page, after which the token is shown once (`test_token_browser_flow_shows_the_token_once`). *Design note:* putting a token into an AI transcript would leak it, so the tool returns a link instead.
- ✅ **S3 Scopes everywhere.** `Principal` is threaded through REST (`Authed(scope, …)`) and every MCP tool. OAuth tokens (`inkcast`) imply every scope. A dry run needs only `posts:read`.

## F2.2 Publish ledger
- ✅ **S1 Ledger table**, keyed `(user, source, path, platform)`.
- ✅ **S2 `POST /api/v1/stories:sync`** returns create / update / unchanged / stale / skipped / failed. A claim before sending and `Idempotency-Key` replay make it duplicate-safe (`test_sync_creates_then_skips_then_updates_never_duplicates`, `test_idempotency_key_replays_instead_of_resending`).
- ✅ **S3 `GET /api/v1/ledger`.**

## F2.3 Updates in place
- ✅ **S1** `Platform.capabilities`, `update()` and `find_existing()`.
- ✅ **S2 DEV** updates with `PUT /articles/{id}` and looks posts up with `GET /articles/me/all`.
- ✅ **S3 Medium** reports `stale` with the post URL, never re-posts, and clears the note when the file is reverted.

## F2.4 Preview and dry run
- ✅ **S1 `POST /api/v1/preview`** uses the same `prepare()` as publishing.
- ✅ **S2 Dry-run sync** changes nothing, ledger included (`test_dry_run_changes_nothing`).

## F2.5 CLI and GitHub Action
- ✅ **S1 `inkcast sync`.**
  - Globs; the token is read from the environment only.
  - Batches of 50, each with an Idempotency-Key.
  - Raw-GitHub image URLs per file directory.
  - Writes the job summary and step outputs.
  - Exit codes 0/1/2 (`test_cli_syncs_a_repository`).
- ✅ **S2 Composite action `action/`.** Dry run on pull requests by default; outputs `results` and `urls`.
- ✅ **S3 PR preview comment**, enabled with `comment: true`.
- ✅ **S4** Example workflow (`docs/examples/publish-on-push.yml`) and the user guide (`docs/git-publishing.md`).

## F2.6 Pipeline hardening
- ✅ **S1 No blind retries.** Unconfirmed results are checked with `find_existing` before anything is retried (`test_unconfirmed_publish_is_checked_before_anything_is_retried`).
- ✅ **S2 `unknown` → `unconfirmed`** in the API, the MCP output and stored data (migration).
- ✅ **Scheduled files** are re-scheduled when content or `publish_at` changes before they go out (`test_scheduled_files_are_rescheduled_when_they_change`). Any `publish_at` is a scheduled post at the time written, due at once if it has passed ([ADR-0015](../../adr/0015-publish-at-is-always-scheduled.md), `tests/test_sync_always_schedule.py`).
- ⏭ **Idempotency keys passed to platforms that accept one.** Neither Medium nor DEV accepts one; revisit per platform in E8.

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)
