# Video platforms, batch review and the engagement inbox

**Status:** design. **Sources:** official API references read on 2026-10-22 (linked in each section). Re-check limits, fields and review rules at build time.

This extends the [social platforms epic](social-platforms-epic.md). It adds two video platforms (YouTube, Facebook Pages), a **batch review queue**, and the constraints for a later **engagement inbox**. It also changes the epic's build order.

## Why

Video-first creators and small teams publish several finished items a day across YouTube, Instagram, Facebook and TikTok, often drafted with an AI assistant. Two gaps stop them using Inkcast today:

- **No YouTube or Facebook.** Long-form and short video on YouTube, and Reels on Facebook Pages, are where much of that audience is.
- **Review is one post at a time.** The composer and consent records (issue 20) work per post. Approving a day's queue means opening each item separately.

## Build order (changes the epic's sequencing)

| Order | Platform | Why now | Main gate |
|---|---|---|---|
| 1 | **Instagram** | Stable container flow, free | Meta App Review + Business Verification |
| 1b | **Facebook Pages** | Same Meta app and Business Verification as Instagram; Reels share the media pipeline | Same Meta review (add the Pages permissions to it) |
| 2 | **YouTube** | Free API; an upload costs ~100 quota units since 2025-12-04 | Google OAuth verification; YouTube API compliance audit for more quota |
| 3 | **TikTok** | Most UX obligations | Content Posting audit (private-only until then) |
| 4 | **X** | Text-first; every post with a link costs money | Paid credits |

Start the calendar-time gates in week 1, in parallel: Meta Business Verification (covers Instagram and Facebook), the Google Cloud project and OAuth consent screen, TikTok app registration and media-domain verification.

## YouTube plug-in

Reference: [videos resource](https://developers.google.com/youtube/data/reference/rest/v3/videos), [revision history](https://developers.google.com/youtube/data/revision_history).

**Connect.** `OAuthConnect` (issue 16) with Google: PKCE, offline access for a refresh token, least scopes (`youtube.upload`, plus `youtube.readonly` for the channel name and the post-publish permalink). The YouTube scopes need Google's OAuth app verification before the public can connect; check their current classification when submitting.

**Upload.** The resumable upload protocol (chunks in multiples of 256 KiB) streamed from object storage (issue 18). An interrupted upload resumes from the last acknowledged byte. It maps onto the async state machine (issue 19): `preparing` while bytes go up, `processing` until YouTube's processing finishes, then `published` with the watch URL.

**Composer fields** (issue 20). Each is an explicit choice; none is set silently:

| Field | API property | Rule |
|---|---|---|
| Audience | `status.selfDeclaredMadeForKids` | Required, **no default**. Child-directed content must be marked; that is the creator's legal call, not Inkcast's |
| Altered or synthetic content | `status.containsSyntheticMedia` | Shown on every upload. Required when the workspace or the post says the media is realistic AI-generated or altered |
| Visibility | `status.privacyStatus` | **No default** in the composer (the API defaults to `public`) |
| Title, description, tags, thumbnail, playlist | `snippet.*`, `thumbnails.set`, `playlistItems.insert` | Platform-correct lengths checked in `prepare()` |

**Scheduling.** Inkcast's own scheduler publishes at time T and re-validates first (fail closed), the same as every platform. YouTube's native `status.publishAt` (only valid while the video is private) is a capability flag for later, for people who want the upload done hours ahead.

**Shorts.** There is no separate endpoint; YouTube classifies vertical, short videos as Shorts. `prepare()` warns when a video meant as a Short is not vertical or exceeds YouTube's current Shorts length.

**Quota.** Since 2025-12-04 an upload costs about 100 units instead of about 1,600, and since 2026-06-01 `videos.insert` draws from its own quota bucket. The `quota_ledger` (issue 21) reserves per Google Cloud project before sending, and `quota()` reports what's left. More quota goes through the YouTube API compliance audit.

**Audit.** The 2020-07-28 rule restricted uploads from unverified API projects to private. The current `videos` reference words this differently. Confirm the rule before building, and plan the compliance audit regardless.

**Delete and edit.** `videos.delete` and metadata edits through `videos.update` are both supported (capability flags on).

## Facebook Pages plug-in

Reference: [Reels Publishing API](https://developers.facebook.com/documentation/video-api/guides/reels-publishing).

- **Connect.** The same Meta app as Instagram. Permissions `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`; a Page access token from someone who can create content on the Page.
- **Reels.** `POST /{page-id}/video_reels` with `upload_phase=start`, then the bytes to `rupload.facebook.com` (resumable by `offset`) or a public `file_url` pull from the media domain, then publish. The reference's example and parameter table disagree on the last phase's value (`finish` vs `complete`): check it at build time.
- **Specs.** 9:16, 1080×1920 recommended, 3–90 seconds.
- **Limit.** 30 API-published Reels per rolling 24 hours per Page. The quota ledger enforces it before sending.
- **Labels.** `is_ai_generated` is a composer field, never set silently.
- **Scheduling.** Native scheduling exists (10 minutes to 29 days ahead) but Inkcast's scheduler is used, as for every platform.
- **Later.** Page photo and text posts.

## Batch review queue

A **review** is a set of posts waiting for a decision, reviewed on one screen: the web app's approvals page (E4, F4.9) and the mobile app (issue 13).

```
draft ─► awaiting_review ─► approved ─► scheduled ─► (publish state machine)
                │
                └► rejected (reason) ─► draft
```

**On the screen, per item:** the media player or image, the per-platform preview, every label (AI-generated, synthetic media, made for kids, paid partnership), the scheduled time, the quota it uses and any cost. Actions: approve, edit, reject with a reason. **Approve all unchanged** approves only the items with nothing left to choose.

**Rules**

- **Consent stays per post.** Approving a review writes one consent record per post, each with its own payload hash. An edit changes the hash, so the item needs approving again.
- **TikTok's mandated choices are never batch-filled.** Privacy (no default) and the interaction toggles (unchecked) appear inline on each TikTok item. Approve all skips any item with a required choice still open.
- **Only people with `can_approve`** (OpenFGA, F4.9.S2) see the actions. The AI assistant can submit a review but never approve one.
- **Fail closed at time T.** Approved posts still re-validate before publishing, as in the epic.

**API and MCP**

| Surface | Call |
|---|---|
| MCP | `inkcast_submit_for_review(posts)` returns a one-time review link; it never approves |
| REST | `POST /api/v1/reviews` · `GET /api/v1/reviews/{id}` · `POST /api/v1/reviews/{id}/decisions` (per-item approve, reject or edit) |
| Events | `review.requested`, `post.approved`, `post.rejected` (extends F4.9.S3) |
| KPIs | review latency (requested → decided), items per review, rejection rate |

## Engagement inbox (later)

Reading comments and messages on connected accounts, with the user's AI assistant drafting replies and a person approving each send. Designed only far enough to set the constraints:

- **Official APIs only.** Instagram comment replies need `instagram_manage_comments` (or `instagram_business_manage_comments`). Messages go through Meta's messaging APIs and their reply windows. YouTube replies use `commentThreads` and `comments`. No TikTok comment API is planned.
- **Every send is approved by a person**, through the same review screen. That covers replies, hides and deletes. Nothing is sent unattended.
- **No outbound unsolicited messages** and no bulk replies. Rate limits come from the quota ledger.
- **Escalation.** Messages that mention safety, minors or legal threats are flagged for the person and are never auto-drafted.

## Stories (parked as issues 33–36)

| # | Story | Depends on |
|---|---|---|
| 33 | YouTube plug-in: OAuth, resumable upload, composer fields (audience, synthetic media, visibility), quota, delete and edit | 15–19, 20, 21; Google OAuth verification |
| 34 | Facebook Pages plug-in: Reels publishing, `is_ai_generated`, 30-per-day limit | 15–21; the Meta review shared with Instagram (23) |
| 35 | Batch review queue: reviews API, MCP `inkcast_submit_for_review`, per-post consent, web and mobile screens | 20; F4.9.S2 (`can_approve`); issue 13 for mobile |
| 36 | Engagement inbox (later): read, draft, approve, send through official APIs | 35 |

## Open questions

- **YouTube:** does the unverified-project upload restriction still apply to projects created now? What is the current Shorts length limit at build time?
- **Facebook:** which value ends the Reels upload (`finish` or `complete`)? Can photo and text posts reuse the same Page token without extra review?
- **Batch review:** should "approve all unchanged" be a plan-gated feature (E10), or available to every tier?

---
<!-- inkcast-nav -->
[Docs](../../docs/README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
