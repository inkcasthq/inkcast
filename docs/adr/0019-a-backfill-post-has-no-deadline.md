# ADR-0019: A post made after its time is a backfill: no deadline, and behind every promise

**Status:** accepted, 2026-11-04 (owner) · **Epic:** E2 (Git publishing) · **Issue:** #222 · **Supersedes:** ADR-0015's first consequence (lag, the overdue count and the oldest-due age "measured from when a post could first be sent", and the claim order by the longest wait)

## Context

ADR-0015 made every Git file with a `publish_at` a scheduled post at the time as written, due at once if that time has passed. So that a file dated a week ago did not read as a week late on the tick that sent it, its lag, the overdue count and the oldest-due age were measured from the later of its time and its creation, and the scheduler claimed the post that had waited longest first.

The review of PR #217 found three things wrong with that measure (issue #222):

1. **Large imports page on-call.** Files dated in the past are all due at once and drain at the claim batch, 20 per tick. Measured with the real `run_due`, the real batch and the default tick (20 s, `INKCAST_SCHEDULER_INTERVAL=20` in staging and production), the production grace (`INKCAST_OVERDUE_GRACE=600`) and a virtual clock, with every file dated a day ago and synced at once (`tests/test_always_schedule_followups.py` carries the method):

   | Files | Drain | Last send's lag | Sends over 120 s | `InkcastSloBurnScheduleTimeliness` (critical) | `InkcastPostsOverdue` (critical) | `InkcastQueueHeadLagHigh` (warning) |
   |---|---|---|---|---|---|---|
   | 140 | 120 s | 120 s | 0 | — | — | — |
   | 160 | 140 s | 140 s | 20 (12.5%) | — | — | — |
   | 200 | 180 s | 180 s | 60 (30%) | fires (14.4% burn) | — | — |
   | 500 | 480 s | 480 s | 360 (72%) | fires | — | — |
   | 900 | 880 s | 880 s | 760 (84%) | fires | fires (overdue for 5 min) | — |
   | 920 | 900 s | 900 s | 780 (85%) | fires | fires | fires |
   | 2000 | 1980 s | 1980 s | 1860 (93%) | fires | fires | fires |

   That is with instant sends. A send takes a platform call: at 0.5 s each the burn fires from 140 files and the page from 620; at 1 s each, from 100 and 500. Every one of those pages is for a scheduler at its full rate, with nothing for the on-call to do but watch the drain.

2. **An import delays a promise.** With the longest wait first, a post a person scheduled for 60 s into a 900-file import went out 840 s late: every file's creation came before the post's time, so the post queued behind the whole import. That is a real SLO burn and a real page, caused by the order.

3. **Two clocks in one measure.** `created_at` is stamped by the API pod's clock (`PostsRepo.create_post`), the lag is taken on the scheduler pod's (`run_due`'s `now`). An API clock 130 s behind the scheduler's made every due-at-once post read 130 s late on the tick that sent it: a false burn.

## Options considered

| Option | Why not |
|---|---|
| **1. Keep the measure; add capacity** (a bigger claim batch, or a tick that claims again while the batch comes back full) | The drain is bounded by one send at a time and the platform's rate limit, so capacity moves the page from 200 files to a few hundred or a thousand; it does not remove a page that fires on a healthy scheduler. Worth its own story: a burst of posts a person scheduled for the same minute burns the SLO past 140 posts today, legitimately. |
| **2. Spread a sync's past-dated files at a backfill rate** (the k-th file due at now + k × spacing) | `run_at` would no longer be the time as written, which Git sync compares to tell a changed time from an unchanged file (ADR-0015, #187), and the rate would be a guess at what each platform allows, which the 429 and its `Retry-After` already pace. |
| **3. A post made after its time is a backfill: no deadline, behind every promise** (chosen) | It has no time to be on time for. Nothing it does is a page; the queue's depth and head-of-line lag still show it, and a promise is never queued behind it. |

For the clock, the choice is between the API's (`created_at`), the database's (one clock on PostgreSQL, the writing process's on SQLite) and the scheduler's. The scheduler's is the clock that makes a post late or on time: it decides at each tick what is due. The author's time is absolute (UTC, as written). Measuring one against the other needs no second clock, and no other process's stamp.

## Decision

**A post made after its time (`created_at > run_at`: a Git file dated in the past, or a time moved into the past) is a backfill. A post made before its time is a promise. The scheduler sends every promise whose time has come, the earliest time first, before any backfill; backfill posts go in the order they were made. A promise's lag is the scheduler's clock against the time as written: one measure, one clock. A backfill is measured by no deadline.**

- **The lag** (`inkcast_publish_lag_seconds`, the `schedule-timeliness` SLO; `lag_seconds` on the `post.published` event, the customer's `posts_on_time`, `posts_late`, `on_time_rate` and `schedule_lag_seconds`) is observed for a promise only: `now − run_at` on the scheduler's clock. A backfill's send carries `lag_seconds: null`, so `kpi.rollup` counts it in `posts_published` and in neither on-time nor late.
- **`inkcast_posts_overdue`** (`InkcastPostsOverdue`, critical, pages) counts promises more than `INKCAST_OVERDUE_GRACE` past their time. A backfill is never overdue.
- **`inkcast_posts_due`** (depth) and **`inkcast_posts_oldest_due_age_seconds`** (head-of-line lag, `InkcastQueueHeadLagHigh`, warning) count every due post, a backfill from its creation: an import draining at the batch rate shows there, as a warning, never as a page.
- **The claim order** (`PostsRepo.claim_due_posts`): `ORDER BY created_at > run_at, MAX(run_at, created_at), run_at`. Promises first by time, then backfill by creation.
- **The clock.** The lag and the overdue count compare the scheduler's clock with the time as written and read no other clock. `created_at` only says whether a post was made after its time, and orders backfill: a skew between the API pod's clock and the scheduler's can move a post made within the skew of its own time from one class to the other, and never make a post late. Today every Inkcast pod that touches the database shares one node, so one kernel clock: the data volume is `ReadWriteOnce` and the split services carry a required `podAffinity` to the data owner on `kubernetes.io/hostname` (`scripts/gen-manifests.py`). On PostgreSQL the port stamps `created_at` with the server's clock (ADR-0014, F3.1.S1's checklist).
- **What stays from ADR-0015:** a replacement that keeps its schedule keeps its creation (so a promise the scheduler is behind on stays a promise, and stays as late as it is); a file dated in the past is still a scheduled post, due at once, retried under the policy; nothing is sent before its written time.

## Consequences

- An import of past-dated files, of any size, fires no page. The on-call sees it as head-of-line lag (`InkcastQueueHeadLagHigh`, a warning: from 920 files with instant sends, 620 at 0.5 s a send) and as queue depth, and the SLO's error ratio does not move. Measured after the change with the same method, for every size in the table: no send over 120 s, `inkcast_posts_overdue` 0 throughout; the posts a person scheduled for 60 s, 300 s and 600 s into the 900-file drain go out with lag 0 where they were 840 s, 600 s and 300 s late.
- A stuck scheduler is still paged: `InkcastSchedulerStalled` when its loop stops ticking (a tick that fails does not count), `InkcastPostsOverdue` on any promise it is behind on. A scheduler behind on promises is late in the SLO as before.
- **The customer's KPIs move again** (ADR-0015 moved them the other way): a Git file dated in the past still counts in `posts_scheduled` and, once sent, in `posts_published`, but no longer in `posts_on_time`, `posts_late`, `on_time_rate` or `schedule_lag_seconds`. The on-time rate is the rate of promises kept. A file pushed after the time it names (CI ran late) is a backfill too: sent at the next tick, not measured. The REST field `on_time` and the KPI catalogue say so.
- The runbook (`InkcastPostsOverdue`, `InkcastQueueHeadLagHigh`, `slo-schedule-timeliness`), the lag metric's help text, the KPI catalogue and `docs/git-publishing.md` describe the two classes.
- `tests/test_sync_always_schedule.py::FindingADueAtOncePostIsOnTime` and the two `Measured` tests in `tests/test_review_always_schedule.py` that pinned "lag from creation" now pin "not measured"; `FindingAPostDueFirstIsClaimedFirst` holds under the new order.
- **Open:** whether the customer should see a backfill count of its own (`posts_backfilled`), so that `posts_scheduled − posts_on_time − posts_late` is explained in the insights reply. Not added here: it is a KPI of its own, and the catalogue is the owner's.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
