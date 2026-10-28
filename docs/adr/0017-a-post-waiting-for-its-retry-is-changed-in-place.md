# ADR-0017: A post waiting for its retry is changed in place

**Status:** accepted, 2026-10-27 (owner) · **Epic:** E2 (Git publishing) · **Issue:** #204 · **Supersedes:** ADR-0015's rule for a post waiting for a retry, and the first half of its "Open" bullet (a schedule-only change to a post a person cancelled is still ignored)

## Context

When the scheduler sends a post and the platform answers with a retryable error (a 429), the post goes back to `scheduled` with `run_at` set to the retry's time (`INKCAST_PUBLISH_RETRY_DELAYS`, stretched by the 429's `Retry-After`) and keeps its attempt count. From then on `run_at` is not the time the author asked for, and nothing on the post recorded that time.

Git sync handled an edited file in the same way for every scheduled post: cancel the post and make a replacement at the file's time. For a post waiting for its retry, that went wrong. Measured on main at eea5064, for a post 300 s from its retry:

| Author's change during the wait | What happened |
|---|---|
| Body edit (#204) | Replaced by a post due 601 s ago, attempts 0: sent within 30 s, before the `Retry-After` |
| Time moved to +200 s, before the retry | Replaced at +200 s, attempts 0: sent at 201 s, before the retry |
| `publish_at` removed, or moved into the past (ADR-0015 "Open") | Read as `unchanged`: the file's change was not recorded |

ADR-0015 compared a tried post's file time with its `run_at` and needed a `SCHEDULE_LEAD` tolerance to guess which differences were changes. A replacement at the retry's time instead would have broken its rule that an untried post's `run_at` is the file's time.

## Options considered

| Option | Why not |
|---|---|
| **1. Update the waiting post's content in place** | Fixes the body edit only. A time moved before the retry still sends early, and a removed time still reads as unchanged, because the time asked for is still unknown. |
| **2. Store the time asked for, and keep cancel-and-replace** | The replacement would have to carry the attempt count and the retry's time over to a new row: more state moving between rows, and a new post id for the same send. |
| **3. Both** (chosen) | Fixes all three rows. The scheduler's query and the lag metrics are unchanged, because `run_at` is still when the post is due. |

## Decision

**A post the scheduler has tried, and that waits for its retry, is changed in place. It is never cancelled and replaced. The post records the time it was asked for and the platform's wait separately.**

- `posts.requested_at`: the time asked for: a Git file's `publish_at`, or a person's scheduled time. NULL for a post asked for "now". Written when the post is made.
- `posts.not_before`: the earliest the platform allows a send. Written whenever the scheduler re-queues a post it has tried: by a retry (the retry's time) and by recovery's re-send (that moment).
- An author's change (`PostsRepo.edit_waiting`) sets the content and `requested_at`, and sets `run_at = max(requested_at or now, the wait)`. The wait is the post's current `run_at` when that is later than the time asked for. Otherwise it is `not_before`: the post waits for the time asked for, because an earlier edit moved it past the wait. A scheduler from before the column (a rolling deploy, a rollback) re-queues a post without writing `not_before`, so a stale `not_before` is never trusted over `run_at`. The attempt count, the post id and `created_at` are kept. The write applies only while the post is still `scheduled` with the attempt count the sync read. If the scheduler claimed it, or tried it again, in between, the sync reports `skipped` and the next sync applies the change.
- The file's ledger entry takes the new content in the same transaction. If that write fails, the change rolls back.
- Before the change, the sync checks the connection and the destination (`Publisher.refusal`), as it does for a new post. A refusal leaves both the post and the entry unchanged.
- Git sync compares a tried post's file time with `requested_at`, so `SCHEDULE_LEAD` no longer takes part in deciding what changed.
- **Unchanged from ADR-0015:** nothing is sent before its written time, and a post the scheduler has not tried is still replaced (#224 makes the cancel and the claim one transaction).

## Consequences

- During a wait, an edit never sends sooner than the retry, and never sooner than the time written. The send goes out with the new content and counts toward the same `INKCAST_PUBLISH_MAX_ATTEMPTS`.
- A removed time, or a time moved into the past, is recorded as `update`. The post still goes out at the retry, which is the earliest the platform allows.
- No new event is written for a change in place. The sync's `sync.result` event records the `update`, and `post.scheduled` stays one per post or retry, so `posts_scheduled` is not inflated.
- **A row from before the columns:** a tried post with neither column reads its `run_at` as its `not_before` (the retry's time). Its file time is compared with nothing, so the first sync after the upgrade reports `update` once. That edit keeps the retry, so nothing is sent sooner.
- Tests that moved the clock by rewriting `run_at` now rewrite `requested_at` too: the post's time and the file's are one value.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
