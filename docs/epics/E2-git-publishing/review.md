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

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)
