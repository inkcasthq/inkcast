# ADR-0015: A `publish_at` is always a scheduled post

**Status:** accepted, 2026-10-26 (owner) · **Epic:** E2 (Git publishing) · **Issue:** #205

## Context

Git publishing re-reads files long after their date, so a `publish_at` that had passed was read as "now" (`parse_when(past_is_now=True)`), and the parser's "past" starts two minutes ahead. A time under two minutes away, in a new file or brought forward into that window, therefore became a "now" post: sent by the sync itself, up to two minutes before the time written. A "now" post is sent once and never retried by the scheduler, so a 429 at that moment left it `failed` until the next push.

Issue #187 found the worst case: a sync in the two minutes before a scheduled post's time cancelled the post and sent it at once. PR #194 fixed that case (an unchanged file is left alone whatever the clock, and a body edit keeps the file's time), and its review, PR #203, closed two more (a re-run after a failed replacement, and a time moved later inside the window). The review then asked the question that the fixes had been working around: why does Git sync read any written time as "now" at all? Doing so still sent a brand-new file under two minutes away, and a time brought forward into the window, early and unretried.

## Options considered

| Option | Why not |
|---|---|
| **1. Keep the "now" reading** for a new file and a time brought forward; pin it in tests | Two paths send a Git post: the sync (early, once, never retried) and the scheduler (at its time, retried under the policy and Retry-After). Every fix in #194 and #203 was a special case keeping a file out of the first path. The remaining cases still send up to two minutes early. |
| **2. Always schedule** (chosen) | One path: the scheduler sends every dated post, at its time, with retries. A passed time is a post due at once, sent at the next tick (20 s by default). |

## Decision

**Any file with a `publish_at` becomes a scheduled post at the time as written. Only a file with no `publish_at` is a "now" post.**

The rule, for anyone changing Git sync (`services/publishing/sync.py`) or the parser (`libs/content/story.py`):

- A `publish_at` is read as written (`read_when`), in UTC, and kept: `parse_story(schedule_as_written=True)`. It is never collapsed to "now", whether it has passed or is minutes away. The horizon (`SCHEDULE_HORIZON_DAYS`) still applies.
- The post's `run_at` is that time. A time that has passed is due at once: the scheduler claims it at its next tick, and a rate limit or outage is retried under `INKCAST_PUBLISH_MAX_ATTEMPTS`, `INKCAST_PUBLISH_RETRY_DELAYS` and the 429's `Retry-After`, like any scheduled post.
- **Nothing is ever sent before its written time.** The sync itself sends only a file with no `publish_at`.
- A changed time (earlier, later, into the window or into the past) re-schedules at the new time as written. A removed time makes a "now" post. An unchanged file is unchanged whatever the clock (#187): a post the scheduler hasn't tried is compared with the written time; a post waiting for a retry keeps its retry unless the file names a new time still ahead (by more than `SCHEDULE_LEAD`, the tolerance between the scheduler's clock and the API's).
- `parse_when` keeps its meaning for a person's publish (MCP, REST, the CLI): a past time, or one under `SCHEDULE_LEAD` (two minutes) away, is an error, because there a person is present to omit it and publish now. `past_is_now` is gone: Git sync was its only caller.

## Consequences

- **Lag, the overdue count and the oldest-due age are measured from when a post could first be sent:** the later of its time and its creation. A file dated a week ago is a post created a week after its time; measured from its time alone it read as a week late on the first tick after it was known, which burnt the `schedule-timeliness` SLO (one such post among six or fewer scheduled sends in an hour fires `InkcastSloBurnScheduleTimeliness`, critical, since the error ratio passes 14.4 times the 1% budget), counted as `posts_late` in the customer's on-time rate, and showed as `inkcast_posts_overdue` until the tick (the alert holds for 5 minutes, so it did not page). A post the scheduler is behind on is as late as before.
- **The customer's KPIs move:** a dated file that has passed its time is now a scheduled post (`posts_scheduled`, then `posts_on_time`), where it was a "now" publish before.
- **The `Held` tests change.** PR #194's `test_a_time_moved_inside_the_window_sends_now` and `test_a_time_moved_into_the_past_sends_now`, and PR #203's `test_a_time_brought_forward_inside_the_window_still_sends_now`, pinned the "now" reading of a changed time. They now pin the opposite: the replacement is scheduled at the time written, and goes out at that time, by the scheduler. `test_a_removed_time_sends_now` stands.
- **MCP, REST and the CLI are unchanged.** A person scheduling by hand still gets the "in the past or under 2 minutes away; omit it to publish now" error. The CLI (`inkcast sync`) posts files to `/sync`, so it follows this ADR.
- **A dry run** of a dated file reports `scheduled`, never `preview`, whatever the clock.
- **Open, not changed here:** during a retry wait, a time removed or moved into the past or the window still reads as unchanged (the retry's `run_at` is not the file's time), and a schedule-only change to a post a person cancelled is still ignored (#203, concerns 3 and 5).

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
