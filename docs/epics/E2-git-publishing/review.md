# E2 Git publishing: review

E1 and E2 shipped in one branch and were reviewed together, independently. The full report and how each finding was resolved: **[E1 + E2 review](../E1-foundations/review.md)**.

Findings that concern Git publishing directly:

| ID | Severity | What | Resolution |
|---|---|---|---|
| F-06 | P1 | Crash recovery re-queued posts that were mid-send, so a crash right after the platform accepted a post could publish it twice | Fixed (`c65a6d2`). Interrupted posts are confirmed with `find_existing`; only a post the platform says is missing goes back in the queue, and everything else becomes `unconfirmed` |
| F-10 | P2 | A personal access token was looked up twice per MCP call | Fixed (`a5a3b16`): `TokenVerifier.lookup()` |
| F-11 | P2 | `inkcast sync` would send a token over plain http | Fixed (`a5a3b16`): refused, except to localhost (exit 2) |
| F-12 | P2 | No bound on `/tokens/new` and the other unauthenticated endpoints | Fixed (`7cdcaa7`): per-client token bucket, `INKCAST_RATE_LIMIT_PER_MINUTE` |

Also checked and found correct: `claim_new` before any create in `sync.py`, the Idempotency-Key fingerprint check, PAT scope intersection, `oauth_only` on token management, and the Action keeping the token in the environment and dry-running on pull requests.

## Second review (2026-10-18, `main` at `1940309`)

A second, independent review looked for check-then-act races. All four findings were real; the first review had missed them (it checked the Idempotency-Key fingerprint, but not two identical requests at once). Each fix has a test that fails on the old code.

| ID | Severity | What | Resolution |
|---|---|---|---|
| R2-1 | P1 | Cancel checked `scheduled`, then wrote `cancelled` in a second statement. A scheduler claim between the two let the post go out after the cancel reported success | One conditional update, `cancel_if_scheduled()`; 409 when the post isn't `scheduled` any more. Test: `RaceTests.test_cancel_cannot_race_the_scheduler` |
| R2-2 | P2 | Refresh read the token, then revoked its family, without one transaction. With several workers or replicas, two refreshes could both win and leave two token families. (In one process no `await` separates the steps, so it couldn't happen there) | `rotate_refresh()`: select and delete the family in one `BEGIN IMMEDIATE` transaction (`SELECT … FOR UPDATE` on PostgreSQL). Tests: `test_a_refresh_token_can_be_used_once_even_concurrently`, `test_rotation_is_atomic_across_connections` |
| R2-3 | P2 | When a narrower scope matched nothing, refresh fell back to all the original scopes. The SDK's token endpoint already refuses unknown scopes, so the HTTP path wasn't exposed, but the provider itself was wrong | `invalid_scope` for any scope the token doesn't hold; never a fallback. Test: `test_refresh_never_widens_the_requested_scope` |
| R2-4 | P2 | Idempotency-Key: look up, run, then save. Two identical requests could both run; the second's `skipped` could become the replayed answer | The key is reserved (`INSERT OR IGNORE`) before the sync runs; a twin gets 409 `idempotency_key_in_use`; the key is released if the run fails, and a crashed reservation expires after 15 minutes. Test: `test_idempotency_key_is_reserved_before_the_sync_runs` (single and split mode) |

R2-1 is rated P1 under the [definition of done](../../definition-of-done.md) ("wrong behaviour in a realistic case"): a person who cancels a post expects it not to go out.

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)
