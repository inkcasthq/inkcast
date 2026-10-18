# Inkcast: Publishing on behalf of users to TikTok, X and Instagram

*Research date: 2026-10-18. Official developer docs were read on that date with a web-fetch tool. Most official pages show **no last-updated date**, so "as of" means "as fetched on 2026-10-18". Direct `curl` to docs.x.com was blocked by the sandbox egress proxy, so every page was read through a fetch-and-summarize tool. Quotes are short and paraphrased where marked. Anything not confirmed on an official page is flagged **[UNVERIFIED]**, and disagreements between sources are flagged **[CONFLICT]**.*

---

## TL;DR

| | TikTok | X | Instagram |
|---|---|---|---|
| Publish API | Content Posting API: Direct Post (`video.publish`) or Upload-to-inbox (`video.upload`) | `POST /2/tweets` plus v2 chunked media upload (`/2/media/upload/initialize\|append\|finalize`) | Instagram Platform Content Publishing: `POST /{ig-id}/media` creates a container, then `POST /{ig-id}/media_publish` |
| Auth | OAuth 2.0 auth code, confidential (client_secret). PKCE required only for mobile/desktop. Access token 24 h, refresh token 365 d (may rotate) | OAuth 2.0 auth code + PKCE, confidential "Web App" client. Access token 2 h; refresh via `offline.access` | Instagram Login: IG user token, short-lived 1 h, long-lived ~60 d, refreshable. Facebook Login: FB user and Page tokens; Page tokens have no fixed expiry |
| Gate before public posting for arbitrary users | **TikTok audit.** Unaudited apps: `SELF_ONLY` only, max 5 users per 24 h, accounts must be private | No review step. Need a Developer Console app and prepaid credits; follow automation rules | **Meta App Review (Advanced Access) + Business Verification** |
| Cost | Free (no price published) | **Pay-per-use**: $0.015 per post create, **$0.200 per post create with a URL** | Free (no price published) |
| Posting caps | ~15 posts/day per creator ("typically"); per-app daily active-creator cap; 6 init req/min per token | `POST /2/tweets`: 100 / 15 min per user, 10,000 / 24 h per app | 50 published posts / 24 h per account via `content_publishing_limit` **[CONFLICT: one doc section says 100]**; 400 containers / 24 h |
| Text-only | No (video or photo required) | Yes | No (media required) |
| Async | Yes: poll status or use webhooks | Post is sync; video media processing is async | Container processing is async; poll `status_code` |
| Native scheduling via API | No | No (not on the self-serve v2 API) **[UNVERIFIED]** | No (not documented for IG) |
| Delete / edit via API | No delete or edit scope | Delete: yes. Edit: `edit_options` exists (30 min, 5 edits) | Delete: Facebook Login only (`instagram_manage_contents`). Edit: only `comment_enabled`, no caption edit |

---

## 1. TikTok

### 1.1 Which API publishes
The **Content Posting API** has two modes:
- **Direct Post** (scope `video.publish`) publishes straight to the user's profile.
  - Video: `POST https://open.tiktokapis.com/v2/post/publish/video/init/`
  - Photo: `POST /v2/post/publish/content/init/` with `post_mode=DIRECT_POST` and `media_type=PHOTO`
- **Upload / inbox (draft)** (scope `video.upload`) sends the content to the creator's TikTok inbox. The creator must tap the notification and finish posting inside TikTok.
  - Video: `POST /v2/post/publish/inbox/video/init/`
  - Photo: same `content/init` endpoint with `post_mode=MEDIA_UPLOAD`. The TikTok app must be version ≥ 31.8, otherwise `app_version_check_failed`.

**Direct Post sequence:**
1. `POST /v2/post/publish/creator_info/query/`
2. `init`
3. For `FILE_UPLOAD` only: PUT the file to `upload_url`
4. Poll `POST /v2/post/publish/status/fetch/`, or receive webhooks

Sources: Direct Post reference, Get Started, Photo Post reference, Upload reference.

### 1.2 Auth
- **Authorize URL:** `https://www.tiktok.com/v2/auth/authorize/`. Parameters: `client_key`, comma-separated `scope`, `redirect_uri`, `state`, `response_type=code`, optional `disable_auto_auth`. The old `/auth/authorize/` URL is deprecated.
- **Token URL:** `POST https://open.tiktokapis.com/v2/oauth/token/`, used for both code exchange and refresh. Requires `client_secret`, so this is a confidential client and must run on our backend.
- **PKCE:** `code_verifier` is "Required for mobile and desktop app only". It is not required for web.
- **Lifetimes:**
  - Access token: "valid for 24 hours" (`expires_in` 86400).
  - Refresh token: "valid for 365 days" (`refresh_expires_in` 31536000).
  - Refresh responses can return a **different refresh token**. Always persist the new one.
- **Revoke:** `POST /v2/oauth/revoke/`.
- **Redirect URI rules:**
  - Up to 10 per app, each under 512 characters.
  - Must be absolute and start with `https`.
  - Must be static: no query parameters and no `#` fragments.
  - Must be registered in the Login Kit config.
- **Scopes:** `user.info.basic` (profile), `video.publish` (Direct Post), `video.upload` (inbox drafts). Scopes can be toggleable, meaning the user may deny some, so check the `scopes` value returned in the callback.

### 1.3 Audit / approval
- **App setup:** register the app, add the Content Posting API product, enable the Direct Post configuration, and get the `video.publish` scope approved.
- **Before audit:**
  - "All content posted by unaudited clients will be restricted to private viewing mode."
  - "Unaudited API Clients can allow up to 5 users to post in a 24 hour window."
  - Posts must be `SELF_ONLY`, and the posting account must be private. Otherwise the error is `unaudited_client_can_only_post_to_private_accounts`.
- **After audit:** the "audit to verify compliance with our Terms of Service" lifts these limits.
- **Products TikTok marks as not acceptable:**
  - "An app that copies arbitrary contents from other platforms to TikTok"
  - "A utility tool to help upload contents to the account(s) you or your team manages"
  - Apps "should be intended for a wide audience, not limited to internal groups/private use"
- **Timeline:** TikTok publishes none. A third-party vendor blog (bundle.social, Aug 2026) says product access takes "days" and the content audit takes "weeks, and variable". It also says posts made before the audit stay private after it passes. **[UNVERIFIED: not official]**

### 1.4 Pricing
No pricing appears on any TikTok page fetched, so the API appears to be free. **[UNVERIFIED: no official statement found]**

### 1.5 Rate limits and caps
- **Per-token request limits:**
  - `video/init`, `content/init` and `inbox/video/init`: "6 requests per minute"
  - `creator_info/query`: "20 requests per minute"
  - `status/fetch`: "30 requests per minute"
- **Per creator:** "typically around 15 posts per day/ creator account" through Direct Post. This varies per creator; the error is `spam_risk_too_many_posts`.
- **Per app:** a "24-hour active creator cap", sized from the audit's usage estimates. The error is `reached_active_user_cap`.
- **Inbox flow:** "at most 5 pending shares within any 24-hour period". The error is `spam_risk_too_many_pending_share`.

### 1.6 Content types and media rules
- **Text-only posts are not possible.** A post must be video or photo.
- **Video:**
  - Formats MP4 (recommended), WebM, MOV. Codecs H.264 (recommended), H.265, VP8, VP9.
  - 23–60 FPS. Each side 360–4096 px. Maximum 4 GB.
  - Duration: every creator can post 3 minutes, some can post 5 or 10. The API maximum is 10 minutes.
  - Always check the creator's `max_video_post_duration_sec` from `creator_info`.
- **Photo:**
  - WebP or JPEG, ≤1080p, ≤20 MB each.
  - Up to **35** images per post.
  - **Only `PULL_FROM_URL`** is supported (no file upload for photos).
  - `title` ≤ 90 UTF-16 runes; `description` ≤ 4000.
- **Video caption (`title`):** ≤ 2200 UTF-16 runes. Hashtags and @mentions in it are parsed.
- **Supplying media:**
  - **`FILE_UPLOAD`** (chunked PUT to `upload_url`):
    - Files under 5 MB go up whole, in one chunk.
    - Each chunk is 5–64 MB; the last chunk may be up to 128 MB.
    - Files over 64 MB must be split. Chunk count is 1–1000.
    - Chunks must be sent in order, with a `Content-Range` header on each.
    - `upload_url` is "valid for one hour".
  - **`PULL_FROM_URL`:**
    - The URL must be on a **verified domain** (DNS signature recommended; covers subdomains) or a **verified URL prefix**.
    - HTTPS only. No redirects: any 3xx makes the URL invalid.
    - It must stay reachable for the download, which "times out one hour after the download task is initiated".
    - The error is `url_ownership_unverified`.
- **Other post options:**
  - `video_cover_timestamp_ms` sets the cover frame.
  - `disable_comment`, `disable_duet`, `disable_stitch` control interactions.
  - `brand_content_toggle` (required) and `brand_organic_toggle` handle commercial disclosure.
  - `is_aigc` adds the "Creator labeled as AI-generated" tag. This is relevant to Inkcast: expose it to the user.
- **Not documented on the pages fetched:** alt text, clickable links, and carousels beyond multi-photo posts.

### 1.7 Sync or async, status and permalink
- Publishing is **asynchronous**. `status/fetch` returns one of:
  - `PROCESSING_UPLOAD`
  - `PROCESSING_DOWNLOAD`
  - `SEND_TO_USER_INBOX`
  - `PUBLISH_COMPLETE`
  - `FAILED`, with a `fail_reason`. Retryable: `internal`, `video_pull_failed`, `photo_pull_failed`. Do not retry: `auth_removed`, `publish_cancelled`, `spam_risk_*`.
- **Permalink:** `publicaly_available_post_id` (sic) is returned "only when the post is published for public viewership" and has passed moderation. Moderation usually finishes "within one minute" but can take "a few hours". A `SELF_ONLY` post never gets a public ID. **[UNVERIFIED]** The docs do not give a permalink format.
- **Webhooks:**
  - `post.publish.failed`
  - `post.publish.complete`
  - `post.publish.inbox_delivered`
  - `post.publish.publicly_available`
  - `post.publish.no_longer_publicaly_available`

### 1.8 Terms and UX requirements (Direct Post guidelines, followed precisely)
**Account and upload page**
1. Fetch `creator_info` **fresh every time** the "Post to TikTok" page renders.
2. Show the creator's **nickname** so the user knows which account will receive the post. The response also includes `creator_avatar_url`, which "expires after 2 hours".
3. Check the video length against `max_video_post_duration_sec`.
4. If `creator_info` says the creator can't post right now, block publishing and ask the user to try later.

**Post settings**
5. The **title** must be editable by the user.
6. **Privacy level:**
   - Show a dropdown built only from `privacy_level_options`.
   - **No default value.** The user must pick one.
   - A mismatched value fails with `privacy_level_option_mismatch`.
7. **Allow Comment / Duet / Stitch:**
   - Show one checkbox for each, **all unchecked by default**. The user turns each on manually.
   - Grey out any interaction that `creator_info` reports as disabled.
   - Photo posts show only "Allow Comment".

**Commercial content disclosure**
8. Show a commercial content toggle, **off by default**. When the user turns it on:
   - Show the checkboxes "Your brand" and "Branded content". Both may be checked.
   - At least one must be checked, or publishing is disabled. The hover text is "You need to indicate if your content promotes yourself, a third party, or both."
   - Label text by selection:
     - Your brand: "Your photo/video will be labeled as 'Promotional content'"
     - Branded content or both: "…labeled as 'Paid partnership'"
9. **Branded content cannot be private.** When Branded content is checked:
   - Disable "only me", with the hover text "Branded content visibility cannot be set to private."
   - Or switch visibility to public automatically and tell the user.

**Consent text before the publish button**
10. Show the declaration that matches the selection:
    - Default or Your brand: "By posting, you agree to TikTok's Music Usage Confirmation"
    - Branded content: "By posting, you agree to TikTok's Branded Content Policy and Music Usage Confirmation."
    - The policy names must be links.

**Before and after sending**
11. Show a **preview** of the content.
12. **Send nothing to TikTok until the user has expressly consented** to the upload.
13. Don't add watermarks, logos, brand names, links or promotional text to the content.
14. The user must be able to edit any preset text, including the title and hashtags. This matters for AI-drafted captions.
15. Tell the user that processing can take a few minutes. Show progress from polling or webhooks.

**Implication for scheduling:** TikTok's guideline pages don't mention scheduling. Requirement 1 says to re-fetch `creator_info` when the page renders, and privacy options can change between composing and posting time T. The safest design:
- Run the full composer, with consent captured, at scheduling time.
- Re-query `creator_info` at time T.
- If the privacy option or duration rules no longer hold, abort and notify the user rather than substituting a value.

**[UNVERIFIED]** The docs don't say whether auditors accept a deferred publish after consent; ask during the audit.

### 1.9 Native scheduling
None. No schedule parameter exists in the Direct Post or Photo references. Inkcast must hold the media and call `init` at time T. With `PULL_FROM_URL`, the media URL must still be live on our verified domain at that moment.

### 1.10 Delete / edit
None of the scopes allows deleting or editing posts. The scope reference lists only `user.info.basic`, `video.publish`, `video.upload`, `video.list` and similar. Users must delete or edit inside TikTok.

---

## 2. X (Twitter)

### 2.1 Which API publishes
- **`POST /2/tweets`** creates a post. Body fields:
  - `text`, `media.media_ids` (1–4)
  - `reply.in_reply_to_tweet_id`
  - `poll`: 2–4 options of 1–25 characters each; `duration_minutes` 5–10080
  - `reply_settings`, `edit_options.previous_post_id`
  - `made_with_ai`, `paid_partnership`, `community_id`, `geo`, and others
  - `quote_tweet_id` "Requires an Enterprise plan" and isn't available on self-serve pay-per-use.
  - Unknown fields are rejected (`additionalProperties: false`).
- **Media** uses the **v2 chunked upload** (single-shot `POST /2/media/upload` also exists):
  - `POST /2/media/upload/initialize` returns a `media_id`.
  - `POST /2/media/upload/{id}/append`, segments indexed from 0.
  - `POST /2/media/upload/{id}/finalize`.
  - `GET /2/media/upload?command=STATUS` polls processing.
  - Don't send the legacy `command=INIT/APPEND/FINALIZE` style to `POST /2/media/upload`.
- **v1.1 media upload is deprecated.** Emplifi, a third-party source, dates the deprecation 9 June 2025. **[UNVERIFIED on docs.x.com]** Build on v2 only.
- **Alt text:** `POST /2/media/metadata` with `alt_text.text` ≤ 1000 characters, scope `media.write`.

### 2.2 Auth
- **Flow:** OAuth 2.0 Authorization Code with PKCE (`code_challenge_method` S256 or plain).
  - Authorize URL: `https://x.com/i/oauth2/authorize`
  - Token URL: `https://api.x.com/2/oauth2/token`
  - The delete-post spec lists the authorize URL as `https://api.x.com/2/oauth2/authorize` **[CONFLICT, minor]**.
- **Client type:** register a **Web App (confidential client)**. It gets a client secret and authenticates to the token endpoint with an Authorization header. Native and SPA apps are public clients, PKCE only.
- **Tokens:**
  - The access token is valid "for two hours" unless you request `offline.access`, which returns a refresh token (`grant_type=refresh_token`).
  - The docs don't say whether refresh tokens rotate. Treat them as rotating and always store the newest one.
- **Authorization code:** "a time limit of 30 seconds". Exchange it immediately on the server.
- **Scopes for Inkcast:** `tweet.read tweet.write users.read media.write offline.access`. `POST /2/tweets` lists `tweet.read`, `tweet.write` and `users.read`.
- **Callback URLs:**
  - Exact match, including trailing slashes.
  - At most **10** per app.
  - `https` in production. Use `http://127.0.0.1` for local development, not `localhost`.
  - `state` up to 500 characters.
- OAuth 1.0a 3-legged user context also still works.
- Changing app permissions requires users to re-authorize.

### 2.3 Approval
- No app review step is documented. Sign in at console.x.com, create an app, and buy credits.
- Automation rules and developer guidelines still apply, and apps can be suspended.
- AI **reply** bots need "prior written and explicit approval from X". Inkcast should not auto-reply.

### 2.4 Pricing and tiers (critical)
**Official pricing page** (docs.x.com/x-api/getting-started/pricing, no date shown, "Prices are subject to change", the console shows current rates):
- **Model:** "No subscriptions—pay only for what you use." Credits are prepaid in the Developer Console.
- **Write prices:**
  - Post create: **$0.015** per request
  - **Post create with URL: $0.200** per request
  - Post create (summoned): $0.010
  - Interaction delete: $0.010
  - Media Metadata: $0.005
- **Read prices:** Posts $0.005 per resource; user lookup $0.010.
- **Media upload:** no separate price listed. **[UNVERIFIED: may be free or bundled]**
- **Controls and caps:**
  - A per-billing-cycle spend limit; requests are blocked once it is reached.
  - Auto-recharge is available. Requests are blocked while the balance is negative.
  - The only stated cap is 3M Post reads per month. Enterprise covers higher volume.
- The page also mentions a "$20 X API credit" incentive for new paying users.

**History** (secondary sources; the official forum posts were not fetched):
- **Feb 6, 2026:** pay-per-use launched (Gigazine).
- **Apr 16–20, 2026:** link-post price raised to $0.20 (TechCrunch, Apr 22, 2026).
- **After Jun 1, 2026:** legacy Basic plans migrated to pay-per-use (devcommunity.x.com post cited by a third party).
- **After Sep 1, 2026:** legacy Pro plans migrated (opentweet.io).
- Free access now remains only for "Public Utility" apps.

**[CONFLICT]** TechCrunch reported the general post price rising to $0.105 in April 2026. The official pricing page currently says $0.015. **Trust the console.**

**Product implication:** posts containing links cost about 13× more. Show the per-post cost, or absorb it in pricing. Consider counting link posts separately in billing.

### 2.5 Rate limits / caps (official rate-limits page)
| Endpoint | Per user | Per app |
|---|---|---|
| `POST /2/tweets` | 100 / 15 min | 10,000 / 24 h |
| `DELETE /2/tweets/:id` | 50 / 15 min | n/a |
| `POST /2/media/upload` | 500 / 15 min | 50,000 / 24 h |
| `initialize`, `append`, `finalize` (each) | 1,875 / 15 min | 180,000 / 24 h |
| `GET /2/media/upload` (status) | 1,000 / 15 min | 100,000 / 24 h |

- The **10,000 posts per 24 h per app** limit is the main scaling ceiling for a multi-tenant SaaS.
- **[UNVERIFIED]** Whether X's separate account-level daily posting limits (help-center rules) also apply to API posts.

### 2.6 Content and media rules
- **Text:**
  - Text-only posts are allowed.
  - 280 weighted characters. CJK and emoji count as 2. **Every URL counts as 23** (t.co wrapping).
  - The docs fetched don't mention longer posts for Premium. **[UNVERIFIED]**
  - Self-serve accounts get one cashtag per post.
- **Media:**
  - Up to 4 photos, **or** 1 GIF, **or** 1 video.
  - Images ≤ 5 MB; GIF ≤ 15 MB.
  - Video: 20 min / 8 GB by default; 125 min / 16 GB for Premium or verified users. Limits follow the **posting user's** account, not the developer plan.
  - Too-long videos get a 403 at post time, even after a successful upload. Check the user's limit before uploading.
- **Uploads:**
  - Chunks ≤ 5 MB recommended; 8 MB server maximum.
  - Media IDs expire after `expires_after_secs` (86400 in examples). Upload close to time T or within 24 h of it.
- **Media supply:** upload only. No URL pull, so Inkcast must stream bytes from our storage.
- **Threads:**
  - Chain posts with `reply.in_reply_to_tweet_id`.
  - Since about Feb 23, 2026, self-serve replies are allowed "only if the original post's author has explicitly summoned the replying account".
  - Replying to **your own** post (self-threads) is reportedly unaffected (opentweet.io). **[UNVERIFIED on official docs]** Test this before promising thread support.
- **Labels:** `made_with_ai` and `paid_partnership` flags exist. Expose them in the composer.

### 2.7 Sync or async
- `POST /2/tweets` is **synchronous**. It returns 201 with `data.id` and `data.text`.
- **Permalink:** `https://x.com/{username}/status/{id}`, using the username from `users.read`. **[UNVERIFIED: permalink format not stated on the pages fetched]**
- **Async parts:** video and GIF processing after `finalize`. Poll the status endpoint, honoring `check_after_secs`, until `succeeded` or `failed`.
- **Webhooks:** the pricing page lists `post.create` webhook events at $0.005 each. They aren't needed for our own posts.

### 2.8 Terms and policy (automation rules + developer guidelines)
**Consent**
- OAuth "does not by itself constitute sufficient consent". You must:
  - clearly describe the automated actions,
  - get express consent,
  - "immediately honor a user's request to opt-out",
  - get consent again after a substantial change in functionality.

**Prohibited behavior**
- "duplicative or substantially similar posts on one account or over multiple accounts", and "No identical cross-posting". This matters for Inkcast if one customer pushes the same text to several X accounts.
- Automatically posting about trending topics.
- Unsolicited automated replies or mentions.
- Automated likes. Bulk reposting.
- Scraping or browser automation instead of the API.
- Duplicate apps to bypass limits.
- Training ML models on X data (Grok excepted).

**Automated accounts**
- Must turn on the "Automated" label and disclose the operator. This targets bots; a human-authored, scheduled post is arguably not a bot. **[UNVERIFIED how it applies to scheduling tools; X's page doesn't mention scheduling]**

**Design implications**
- The user approves every post.
- Don't auto-reply.
- Warn users about duplicate content across accounts and times.

### 2.9 Native scheduling
- No schedule parameter exists on `POST /2/tweets`, so Inkcast schedules.
- X's scheduled posts are an Ads API and X Pro feature, outside this API. **[UNVERIFIED]**
- The edit-posts page says "scheduled posts" are not editable. This refers to X's own scheduler.

### 2.10 Delete / edit
- **Delete:** `DELETE /2/tweets/{id}`, own posts only, 50 / 15 min per user.
- **Edit:** `edit_options.previous_post_id` exists on `POST /2/tweets`.
  - Window: "30 minutes from original post", "5 edits maximum".
  - Polls, replies to others, reposts and some other post types can't be edited.
  - The edit-posts page doesn't state a Premium requirement. In the app, editing has historically been a Premium feature. **[UNVERIFIED for API]**
  - Each edit is presumably billed as a post create. **[UNVERIFIED]**

---

## 3. Instagram

### 3.1 Which API publishes
The **Instagram Platform Content Publishing API** has two setups. **An app can use one, not both** (App Review page).

| | Instagram API with **Instagram Login** | Instagram API with **Facebook Login for Business** |
|---|---|---|
| Host | `graph.instagram.com` | `graph.facebook.com` and `rupload.facebook.com` |
| Token | Instagram User token | Facebook User or Page token |
| Facebook Page needed | No | Yes (IG account linked to a Page) |
| Publish permissions | `instagram_business_basic`, `instagram_business_content_publish` | `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` (plus `pages_show_list`; plus `ads_management`/`ads_read` if the user has a Business Manager role on the Page) |
| Resumable (local-file) video upload | **No** | Yes |
| Delete media | **No** | Yes (`instagram_manage_contents`, since Dec 3, 2025) |
| `media_product_type` field | No | Yes |
| Audio API (Jun 2026) | No | Yes |

- **Account type:** Instagram **professional accounts**, Business or Creator. Personal accounts are not supported.
- **PPA:** if the IG account's linked Page requires Page Publishing Authorization, publishing is blocked until PPA is completed.

**Flow**
1. `POST /{ig-id}/media` creates a container. Pass `image_url` or `video_url`, plus `media_type` = `REELS`, `STORIES` or `VIDEO`, or a carousel `children` list.
2. Poll `GET /{container-id}?fields=status_code` until `FINISHED`.
3. `POST /{ig-id}/media_publish` with `creation_id` returns the IG media ID.

- Containers expire after 24 h.
- Check usage with `GET /{ig-id}/content_publishing_limit`.

### 3.2 Auth
**Instagram Login (Business Login for Instagram)**
- **URLs:**
  - Authorize: `https://www.instagram.com/oauth/authorize`
  - Code exchange: `POST https://api.instagram.com/oauth/access_token`. Needs client secret, so this is a confidential server-side client.
- **Code:** "valid for 1 hour and can only be used once". Strip the trailing `#_` from it.
- **Tokens:**
  - The short-lived token is valid for one hour. Exchange it for a **long-lived token of about 60 days** (`expires_in` ≈ 5,183,944 s).
- **Refresh:** `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token`.
  - The token must be at least 24 h old and not yet expired.
  - The user must have granted `instagram_business_basic`.
  - "Tokens that have not been refreshed in 60 days will expire and can no longer be refreshed." **Inkcast needs a refresh job.**
- **Redirect URI:** must exactly match a valid OAuth redirect URI in the App Dashboard. Watch for trailing slashes.
- **Scopes:** old `business_*` scope names were deprecated Jan 27, 2025. Use `instagram_business_*`.
- **PKCE:** not mentioned in the docs fetched. **[UNVERIFIED]**

**Facebook Login**
- Long-lived user token is about 60 days. The SDKs refresh it automatically if the user was active in the last 90 days.
- **Page tokens** fetched with a long-lived user token "do not have an expiration date". They can still be invalidated, for example by a password change or a role removal.

### 3.3 App Review / Business Verification
- **Standard Access** covers only accounts with a role on the app, so it is useful for testing.
- Serving accounts we don't own needs **Advanced Access**, which "requires App Review and Business Verification".
- **Business Verification:**
  - Required for Advanced Access since Feb 1, 2023.
  - Only a Business Admin can complete it.
  - The docs list no document requirements or timelines.
- **App Review submission:**
  - One screencast per permission showing the end-to-end flow.
  - English UI, or captions.
  - Step-by-step reviewer login instructions and test credentials.
  - At least one successful API call per permission requested.
  - If reviewers can't access the app, "your entire submission will be rejected".
  - Meta publishes no review timeline. **[UNVERIFIED: plan for weeks and at least one rejection cycle]**
- **[CONFLICT, naming]** The App Review page lists the permissions as `instagram_business_content_publishing` and `instagram_content_publishing`. The permissions reference and the publishing guide say `instagram_business_content_publish` and `instagram_content_publish`. Use the names shown in the App Dashboard.

### 3.4 Pricing
No fee is published; the API is free. Webhooks are free. **[UNVERIFIED: no official statement found]**

### 3.5 Rate limits / caps
- **Publishing cap [CONFLICT]:**
  - `content_publishing_limit` returns `quota_total` **50** per `quota_duration` 86400 s, described as "currently".
  - The publishing guide's "Rate Limit" section says "**100** API-published posts within a 24-hour moving period".
  - The same guide's carousel section says "50 published posts within a 24-hour period".
  - **Read `content_publishing_limit` at runtime** and enforce the returned value in our scheduler, as Meta recommends. Carousels count as 1 post.
- **Containers:** "400 containers within a rolling 24-hour period". Containers expire after 24 h.
- **General calls:** Business Use Case limits; "Calls within 24 hours = 4800 * Number of Impressions".

### 3.6 Content types and media rules
- **Media is required.** No text-only posts.
- **Supported content:**
  - Single image, single video (Reels), carousels of up to 10 items (images and/or videos, no Reels inside), Stories (image or video).
  - Trial Reels (`trial_params`, Dec 2025).
- **Media must be at a publicly accessible URL** "at the time of the attempt". Meta fetches it (cURL).
- **Resumable upload** (`upload_type=resumable` to `rupload.facebook.com`, local bytes or `file_url`) is available **only with Facebook Login**.
- **Images:**
  - **JPEG only** (no MPO or JPS), ≤ 8 MB, sRGB.
  - Aspect ratio **4:5 to 1.91:1**; width 320–1440 px.
  - Carousel items are cropped to the first image's ratio (default 1:1).
- **Reels:**
  - MOV or MP4 (moov atom at the front, no edit lists). HEVC or H.264, closed GOP, 4:2:0. AAC ≤ 48 kHz.
  - 23–60 FPS. ≤ 1920 px wide. ≤ 25 Mbps VBR.
  - **3 s – 15 min**, ≤ **300 MB**. 9:16 recommended.
  - Cover via `cover_url` (JPEG ≤ 8 MB) or `thumb_offset`. `share_to_feed` and `audio_name` are optional.
- **Story video:** 3–60 s, ≤ 100 MB. Story image: JPEG ≤ 8 MB.
- **Caption:** "Maximum 2200 characters, 30 hashtags, and 20 @ tags". Carousel children have no captions; only the carousel container takes one.
- **Alt text:** `alt_text` ≤ 1000 characters. **Images only**, not Reels or Stories (since Mar 24, 2025).
- **Other fields:**
  - `collaborators`: up to 3 usernames; feed, Reels and carousels.
  - `user_tags`, `location_id`.
  - `product_tags`: max 5, Facebook Login and shopping only.
  - **Paid partnership label:** `is_paid_partnership` and `branded_content_sponsor_ids` (Apr 22, 2026).
  - **AI self-disclosure:** `is_ai_generated` (Jun 22, 2026). For carousels, set it on the parent container only. Expose it in the composer.
- **Not supported:** shopping tags (Instagram Login), branded content tags (per the guide's limitations section), filters.
- **Links in captions are not clickable.** This is standard Instagram behavior, but **[UNVERIFIED]** no official Meta page saying so was found. Treat any URL in a caption as plain text and warn the user.

### 3.7 Sync or async
- Container creation is **asynchronous**, especially for video. `status_code` is one of `IN_PROGRESS`, `FINISHED`, `ERROR`, `EXPIRED`, `PUBLISHED`.
- Meta's guidance: if `media_publish` doesn't return an ID, poll "once per minute for no more than 5 minutes".
- `media_publish` returns the IG media ID. Then `GET /{ig-media-id}?fields=permalink,shortcode` gives the public URL. `permalink` is not available on carousel children.
- **No webhook for publish completion.** Webhook fields cover comments, mentions, messages and `story_insights`. Use polling.

### 3.8 Terms and policy
- **Meta Developer Policies:**
  - 1.7: "Obtain consent from people before publishing content or taking any other action on their behalf."
  - 1.4: "Don't confuse, deceive, defraud, mislead, spam or surprise anyone."
  - 1.8: offer an easy support channel.
  - 6.2: don't use the Instagram Platform to import or back up content without permission.
  - 9.2: don't use the API to publish looping videos or static, animated or looping images (Live API context).
- **Allowed usage** of `instagram_business_content_publish`: "Manage the organic content creation process for Instagram" for business accounts. A scheduler fits this.
- **App Review requirement:** the screencast must show the user choosing the account, composing, and publishing.
- **Privacy policy URL and data-deletion callback:** standard Meta app requirements. **[UNVERIFIED: not on the pages fetched]**
- **AI:** no specific rule found beyond the new `is_ai_generated` disclosure field. Recommend surfacing it.

### 3.9 Native scheduling
None for Instagram. The IG media endpoint has no `scheduled_publish_time`; that parameter exists for Facebook Pages, not IG. Meta's guide says apps should enforce the rate limit themselves "especially for scheduled posts", which confirms the app is expected to do the scheduling.

Design for Inkcast:
- Create the container shortly before time T. Containers expire in 24 h, and the media URL must be live when the container is created.
- Wait for `FINISHED`, then publish at T.
- For video, start the container about 5–15 min early to allow processing.

### 3.10 Delete / edit
- **Delete:** `DELETE /{ig-media-id}`, **Facebook Login only**, permission `instagram_manage_contents` (added Dec 3, 2025).
  - Covers posts, Stories, Reels and whole carousels; not individual carousel items.
  - Not available with Instagram Login.
- **Edit:** only `POST /{ig-media-id}` with `comment_enabled`. **No caption edit** through the API.

---

## 4. What this means for Inkcast's plug-in interface

1. **OAuth connect per platform, no pasted API keys.** Each platform uses a server-side, confidential-client authorization-code flow:
   - TikTok: `client_secret`.
   - X: PKCE + client secret.
   - Instagram: client secret.

   The connector interface needs `beginConnect(userId) → authorizeUrl` and `completeConnect(code, state) → Account`. Also:
   - CSRF `state` plus PKCE verifier storage.
   - Exact, static redirect URIs, at most 10 each on TikTok and X, with no query parameters on TikTok.
   - **Immediate** code exchange. X codes live 30 s.
2. **Token lifecycle and encrypted storage.** Store tokens encrypted (KMS envelope) with `expires_at`, `refresh_expires_at` and the granted scopes. Rotate on refresh: TikTok says so explicitly; treat X the same way. Background jobs:
   - TikTok: access token 24 h, refresh token 365 d.
   - X: access token 2 h, refresh only with `offline.access`.
   - Instagram: long-lived token about 60 d, refreshed between 24 h old and expiry, with a hard stop at 60 d without refresh.

   A connection health state (`ok`, `needs_reauth`, `scope_missing`) must show in the UI. TikTok scopes can be partially denied.
3. **Media pipeline with two delivery modes.**
   - **Public HTTPS URL pull:** Instagram always (Instagram Login); TikTok photos always and video optionally.
   - **Byte upload:** X always (chunked, 8 MB max chunk); TikTok `FILE_UPLOAD` (5–64 MB chunks, sequential); Instagram resumable (Facebook Login only).

   This calls for object storage plus a **CDN domain with no redirects that we control and verify for TikTok** (DNS TXT). Use stable or signed URLs that stay valid for at least 1 h past publish. Also needed:
   - Per-platform transcoding and validation: IG JPEG-only images and 4:5–1.91:1 aspect; X images ≤ 5 MB; TikTok WebP/JPEG photos ≤ 1080p.
   - Duration checks per creator: TikTok `max_video_post_duration_sec`; X Premium vs default limits.
4. **Asynchronous publish state machine.** `scheduled → preparing (upload/container) → processing → published(permalink) | failed(reason, retryable?)`.
   - **Polling:** TikTok status (30/min per token) or webhooks; X media STATUS honoring `check_after_secs`; Instagram `status_code` once per minute for up to 5 min.
   - **Permalink:** fetched after publishing. TikTok's ID arrives only after public moderation.
   - **Retry:** classify retryable vs fatal errors from each platform's documented codes.
5. **Per-platform composer with UX contracts.** A generic "post everywhere" form isn't compliant on TikTok. Each connector must declare a **UI schema**:
   - TikTok's mandatory fields: privacy with no default, interaction toggles off by default, commercial disclosure with mutual exclusions, consent text, nickname display.
   - Capability flags: text-only allowed, max media, threads, alt text, AI-label field (`is_aigc`, `made_with_ai`, `is_ai_generated`), paid-partnership field, delete supported, edit supported.

   AI-drafted captions must stay editable and be explicitly approved by the user. Store a consent record (who, when, which exact payload) for each post. Re-validate at time T (TikTok `creator_info`, IG quota) and fail closed.
6. **Quota and cost enforcement in our scheduler:**
   - IG: read `content_publishing_limit`.
   - TikTok: ~15 posts per creator per day, plus our app-wide active-creator cap.
   - X: 100 / 15 min per user and 10,000 / 24 h per app. **Track dollars too**: $0.015 per post vs $0.20 for a post with a URL, plus a spend limit and auto-recharge on the X console.
   - Warn about duplicate or similar content across X accounts.
7. **Edit and delete are not uniform.** Expose them as capabilities:
   - Delete: X yes, IG only with Facebook Login, TikTok no.
   - Edit: X 30 min, IG comments toggle only, TikTok no.
8. **Instagram Login vs Facebook Login is a one-way choice.** Instagram Login is simpler: no Page needed, and Creator accounts work without one. Facebook Login is needed for **delete**, **resumable local upload** and the Audio API. Recommended:
   - Start with **Instagram Login** for faster onboarding.
   - Accept "no delete via API" for the first version.
   - Revisit if customers need delete. **[Verify]** whether one Meta app can switch later, since the App Review page says an app can use one login type, not both. A second app would mean a second review.

## 5. Sequencing recommendation

| Order | Platform | Why | Main risks |
|---|---|---|---|
| **1. Ship first** | **X** | No review gate. Self-serve console plus credits. Text-only posts. Simple synchronous create. Standard OAuth 2.0 PKCE. | **Ongoing cost** (link posts $0.20; pricing changed twice in 2026 and is "subject to change"). Automation and duplicate-content policy. Self-serve reply ("summoned") restriction may affect threads; test self-threads. The 10k posts / 24 h app ceiling. |
| **2. Build in parallel, launch second** | **Instagram** | Clear, stable container → publish flow, no fees. Start **Business Verification and App Review on day 1**: Standard Access lets you build and record the screencast with your own accounts. | **Approval risk and timeline** (App Review plus Business Verification, no published SLA; whole-submission rejection if reviewers can't log in). Professional accounts only. Public-URL media hosting. 50/day cap ambiguity (100 vs 50). No delete with Instagram Login. |
| **3. Last (highest approval risk)** | **TikTok** | Most UX obligations. Mandatory audit before any public post. Unaudited apps limited to 5 users per 24 h and private-only posts, so a real beta isn't possible before the audit. Pre-audit posts reportedly stay private. | Audit can reject "utility" or internal-use tools and cross-platform "copy" apps. Inkcast must present as a broad-audience creator tool with a full compliant composer. Domain verification for URL pull. No delete or edit. ~15 posts per day per creator. Scheduled deferred publish versus "fresh creator_info" and consent rules: raise this in the audit. |

**Critical-path actions this week:**
- **Meta:** start Business Verification.
- **TikTok:** register the app, add the Content Posting API, enable Direct Post, and verify our media CDN domain.
- **X:** create the console app, fund credits, and set a spend cap.

The audit and review queues, not engineering, set the timeline for IG and TikTok.

---

## 6. Open questions / unverified items
- TikTok audit timeline and whether auditors accept scheduled (deferred) publishing. Ask in the audit submission.
- TikTok API pricing (assumed free; not stated).
- X: whether the media upload cost is $0; whether X refresh tokens rotate; whether self-thread replies are exempt from the "summoned" restriction (third-party claim); whether edit via API needs Premium; the post-create price (official page $0.015 vs TechCrunch $0.105).
- X v1.1 media upload deprecation date (9 Jun 2025 per a third party).
- Instagram: 50 vs 100 per 24 h publishing cap (use the runtime endpoint); permission naming (`*_publish` vs `*_publishing`); PKCE support in Instagram Login; App Review timelines; official source for "caption links not clickable".
- The legacy X Basic/Pro migration dates come from secondary sources citing devcommunity.x.com; the forum posts weren't fetched.

---

## Sources

### TikTok (official)
- https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
- https://developers.tiktok.com/doc/content-posting-api-get-started
- https://developers.tiktok.com/doc/content-posting-api-get-started-upload-content
- https://developers.tiktok.com/doc/content-posting-api-reference-upload-video
- https://developers.tiktok.com/doc/content-posting-api-reference-photo-post
- https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info
- https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
- https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
- https://developers.tiktok.com/doc/content-sharing-guidelines
- https://developers.tiktok.com/doc/oauth-user-access-token-management
- https://developers.tiktok.com/doc/login-kit-web
- https://developers.tiktok.com/doc/tiktok-api-scopes/

### TikTok (secondary)
- https://bundle.social/blog/tiktok-api-approval (vendor blog, Aug 2026: audit timeline and review tips)

### X (official)
- https://docs.x.com/x-api/getting-started/pricing (and .md)
- https://docs.x.com/x-api/posts/manage-tweets/introduction.md
- https://docs.x.com/x-api/posts/manage-tweets/integrate.md
- https://docs.x.com/x-api/posts/create-post
- https://docs.x.com/x-api/posts/delete-post
- https://docs.x.com/x-api/fundamentals/rate-limits
- https://docs.x.com/x-api/fundamentals/edit-posts.md
- https://docs.x.com/x-api/media/quickstart/media-upload-chunked.md
- https://docs.x.com/x-api/media/create-media-metadata.md
- https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code.md
- https://docs.x.com/fundamentals/developer-apps.md
- https://docs.x.com/fundamentals/counting-characters
- https://docs.x.com/developer-guidelines
- https://help.x.com/en/rules-and-policies/x-automation

### X (secondary)
- https://gigazine.net/gsc_news/en/20260209-x-api-pay-per-use (pay-per-use launch, Feb 2026)
- https://techcrunch.com/2026/04/22/x-makes-it-more-expensive-to-post-links-through-its-api/ (link price change, Apr 2026)
- https://opentweet.io/x-api-pricing (legacy Basic/Pro deprecation dates)
- https://www.simpmusic.org/blogs/x-legacy-basic-api-plans-moving-to-pay-per-use (cites https://devcommunity.x.com/t/important-update-legacy-x-api-basic-plans-are-moving-to-pay-per-use-ppu/266305, not fetched)
- https://opentweet.io/how-to/fix-x-api-reply-restriction (summoned-reply restriction, self-threads)
- https://docs.emplifi.io/platform/latest/home/x-formerly-twitter-media-upload-v1-1-api-endpoints (v1.1 media deprecation date)

### Instagram / Meta (official)
- https://developers.facebook.com/docs/instagram-platform/content-publishing
- https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing
- https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
- https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit
- https://developers.facebook.com/docs/instagram-platform/reference/instagram-media
- https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login
- https://developers.facebook.com/docs/instagram-platform/overview
- https://developers.facebook.com/docs/instagram-platform/app-review
- https://developers.facebook.com/docs/instagram-platform/webhooks
- https://developers.facebook.com/docs/instagram-platform/changelog
- https://developers.facebook.com/blog/post/2025/12/03/instragram-api-updates/
- https://developers.facebook.com/docs/permissions
- https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived
- https://developers.facebook.com/docs/development/release/business-verification
- https://developers.facebook.com/docs/resp-plat-initiatives/individual-processes/app-review
- https://developers.facebook.com/devpolicy
