# Epic: X, Instagram and TikTok

**Status:** design. **Research:** [docs/research/social-platforms-2026-10.md](../research/social-platforms-2026-10.md), read from official docs on 2026-10-18. Re-check prices and caps at build time.

## Goal

Writers turn one story into native posts on X, Instagram and TikTok, from their AI assistant or the API. Inkcast meets each platform's rules on:

- consent,
- media formats,
- quotas,
- cost.

## What's different from Medium and DEV

| Area | Medium / DEV (today) | X / Instagram / TikTok |
|---|---|---|
| Connect | Paste an API key on a one-time page | **OAuth per platform** (Inkcast is a registered developer app); tokens expire and refresh |
| Gate | None | **X:** paid credits. **Instagram:** Meta App Review + Business Verification. **TikTok:** audit (private-only until then) |
| Content | Markdown article | **Media first:** IG and TikTok require images or video; X is text plus up to 4 images or 1 video |
| Media delivery | Public URLs in markdown | **Public URL pull** (IG; TikTok photos; optional TikTok video) **and** **byte upload** (X always; TikTok `FILE_UPLOAD`) |
| Publishing | Synchronous | **Asynchronous**: upload or container → processing → published, polled or by webhook |
| Consent | Chat confirmation + dry run | **TikTok mandates its own composer** (privacy with no default, toggles off by default, disclosure, consent text, preview). Logging in with X is not consent to automated posting |
| Cost | Free | **X charges per post**: $0.015, or $0.200 with a URL — and Inkcast's posts usually *are* links |
| Native scheduling | No | No: Inkcast holds the media and posts at time T, re-validating first |
| Delete / edit | Platform UI | Differs per platform, so it is a capability flag |

## Design

### 1. Plug-in interface v2 (backward compatible)

Medium and DEV keep working unchanged. New *optional* parts of `Platform`:

```python
class Platform(ABC):
    capabilities: ClassVar[Capabilities]   # text_only, max_images, video, threads, alt_text,
                                           # ai_label_field, paid_partnership_field, delete, edit,
                                           # requires_web_consent, cost_model
    connect: ClassVar[ConnectMethod]       # CredentialsConnect (today) | OAuthConnect (new)
    composer: ClassVar[ComposerSchema | None]   # platform-mandated fields + rules (TikTok)

    async def live_context(self, creds) -> dict: ...         # e.g. TikTok creator_info, IG publishing quota
    def prepare(self, fields, body, media: list[MediaAsset]) -> Prepared: ...
    async def publish(...) -> PublishResult                  # may return status="processing" + remote_ref
    async def poll(self, creds, remote_ref) -> PublishResult  # NEW: advance an async publish
    async def delete(self, creds, remote_id) -> None          # only if capabilities.delete
    async def quota(self, creds) -> Quota                     # remaining posts, reset time, est. cost
```

**`OAuthConnect`.** Each platform declares:

- authorize URL, scopes, token URL, PKCE on or off;
- `exchange(code, verifier) → TokenSet` and `refresh(TokenSet) → TokenSet`.

`TokenSet` holds `access`, `refresh`, `expires_at`, `refresh_expires_at` and `scopes`, stored encrypted (the KMS envelope, issue 09). Rules:

- **Exchange immediately:** X codes live 30 s.
- **Rotate on every refresh.**
- **Track connection health** (`ok`, `needs_reauth`, `scope_missing`). TikTok scopes can be partially denied.
- **Run a refresh job ahead of expiry.** Expiry windows:

  | Platform | Access token | Refresh |
  |---|---|---|
  | X | 2 h | needs `offline.access` |
  | TikTok | 24 h | refresh token 365 d |
  | Instagram | ~60 d long-lived | refresh after 24 h, before expiry |

### 2. Connect flow (reuses today's one-time link)

1. `inkcast_connect("x")` (or `POST /api/v1/platforms/x/connect-link`) returns a one-time link.
2. The page shows **Continue to X**. Inkcast creates `state` + PKCE, binds them to the ticket, and redirects.
3. The platform redirects to a **static** `https://<app>/connect/oauth/<platform>/callback`. Inkcast exchanges the code at once and stores the encrypted TokenSet. The page shows "Connected as @handle".

Tokens never pass through the AI; the redirect URIs are registered exactly, with no query strings (TikTok's rule).

### 3. Media pipeline

The pipeline has five parts:

- **Upload in.** MCP and REST accept media as base64 (small) or a pre-signed upload URL (large). The assistant never sees platform URLs or tokens.
- **Store.** S3-compatible object storage holds the files:
  - cloud: S3 or GCS;
  - on-prem / air-gapped: **Rook-Ceph** RGW (CNCF graduated), or MinIO.

  Each object is content-addressed (sha256), scanned and EXIF-stripped. Location metadata is removed by default.
- **Derive.** A worker transcodes per platform: IG JPEG at 4:5–1.91:1; X images ≤ 5 MB; TikTok WebP/JPEG ≤ 1080p; video re-encode and duration checks. It runs as a **KEDA-scaled job** (CNCF graduated) on queue depth.
- **Deliver.**
  - **Public pull:** short-lived signed URLs on a **dedicated media domain**. It is separate from the app domain (cookie isolation), serves without redirects, and is DNS-verified with TikTok. URLs stay valid for at least 1 h past the publish time.
  - **Byte upload:** chunked uploads streamed from storage — X 8 MB chunks; TikTok 5–64 MB, sequential.
- **Retain.** Media is kept until the post is published plus N days (configurable), then deleted.

### 4. Publish state machine (extends today's posts table)

```
scheduled ─► preparing ─► processing ─► published (permalink)
    │            │             │      └► failed (code, retryable?)
    │            └─────────────┴──────► unknown (sent, outcome unclear — never auto-retried)
    └► cancelled
```

- **preparing:** media upload or container creation. Ambiguous failures go to `unknown`, the same rule as today's duplicate-safe publishing.
- **processing:** the scheduler polls `poll()` with each platform's backoff:

  | Platform | Poll rule |
  |---|---|
  | X | honour `check_after_secs` |
  | Instagram | ≤ 1/min for 5 min |
  | TikTok | ≤ 30/min per token; webhooks preferred |

- **Permalink:** fetched after publishing. TikTok issues the post id only after moderation.
- **Webhooks** (TikTok first) arrive at `/hooks/<platform>`. They are signature-verified, normalised to **CloudEvents** (CNCF graduated) and fed into the same state machine.

### 5. Consent and AI-drafted content

TikTok requires its own composer, and X's rules separate login from consent to automate. So:

- **Every social post has a consent record:** who, when, the exact payload hash, and the platform options chosen. It is stored with the post.
- **TikTok** always goes through a **web composer** (`inkcast_publish` returns a one-time *review link*). The composer:
  1. fetches `creator_info` fresh and shows the nickname;
  2. offers privacy from `privacy_level_options`, **no default**;
  3. shows Comment/Duet/Stitch **unchecked**;
  4. shows the commercial-content toggle with "Your brand" / "Branded content" (branded can't be private);
  5. shows the exact consent sentence with links, and a preview;
  6. sends nothing before the click.

  Captions the AI drafted are pre-filled but **editable**.
- **X and Instagram** may publish from chat after a `dry_run` preview the user confirms. A workspace setting can force the web composer for them too.
- **Scheduled social posts re-validate at time T:** TikTok `creator_info`, IG `content_publishing_limit`, X spend cap. If anything no longer holds, they **fail closed** and notify; they never substitute values.
- **AI labels and paid-partnership flags** are first-class composer fields (`is_aigc`, `made_with_ai`, `is_ai_generated` per platform). They are never set silently.

### 6. Quotas and cost

**Quotas.** A `quota_ledger` table records every attempt per account and per app, reserved *before* sending:

| Platform | Limit |
|---|---|
| X | 100 / 15 min per user; 10,000 / 24 h per app |
| Instagram | `content_publishing_limit` read at runtime (docs disagree: 50 vs 100) |
| TikTok | ~15/day per creator, plus the app's active-creator cap |

**Cost (X).** Estimated per post: $0.015, or $0.200 with a URL. It is:

- shown in dry runs ("this post costs $0.20 because it contains a link");
- enforced against a **workspace monthly spend cap**;
- reconciled with the X console.

The default X template puts the canonical link in the post because that's the point of an announcement, so the cost is visible up front.

### 7. Content model: from story to social

`inkcast_publish` gains `announce` targets. After the long-form posts succeed, Inkcast builds per-platform social drafts from the story: title, subtitle, canonical URL, cover image, tags → hashtags. The posts in a group carry a dependency (`after: [medium, devto]`), so announcements wait for the URL they link to. Each platform's draft is editable (and, for TikTok, consented) before it goes out.

### 8. Security and operations

- **Least scopes.**
  - X: `tweet.read tweet.write users.read media.write offline.access`.
  - IG: `instagram_business_basic`, `instagram_business_content_publish`.
  - TikTok: `user.info.basic`, `video.publish` (Direct Post).
- **Egress allow-list.** The `egress-*` components gain `api.x.com`, `upload.x.com` (if used), `graph.instagram.com`, `open.tiktokapis.com`, and the media domain. Exact hosts are confirmed during implementation.
- **Metrics:**
  - `inkcast_publish_total{platform,status}` (already exists);
  - `inkcast_publish_duration_seconds`;
  - `inkcast_quota_remaining{platform}`;
  - `inkcast_spend_dollars_total{platform}`;
  - `inkcast_token_refresh_failures_total`.
- **Store-review prerequisites** (all three): a public privacy policy, terms of service, and a working **data-deletion** path (endpoint or instructions). App icon and a demo screencast use Inkcast's own test accounts. *Check each review's current checklist when submitting.*

## Sequencing (from the research)

**Superseded on 2026-10-22:** the order is now Instagram (with Facebook Pages on the same Meta review), YouTube, TikTok, then X. The reasons and the new platforms are in [video platforms, batch review and the engagement inbox](video-platforms-and-review.md). The original table is kept below for its risk notes.

| Order | Platform | Why | Main risk |
|---|---|---|---|
| 1 | **X** | No review gate; text-only works; synchronous create | Ongoing per-post cost; automation and duplicate rules |
| 2 | **Instagram** | Stable container flow, free | App Review + Business Verification: no SLA |
| 3 | **TikTok** | Most UX obligations | Audit gate; deferred publish vs "fresh creator_info"; ask in the audit |

**Start this week (calendar time, not engineering time):**

- Meta Business Verification;
- TikTok app registration + media-domain verification;
- X console app with a spend cap.

## Stories (filed as issues 15–24)

| # | Story | Depends on |
|---|---|---|
| 15 | Plug-in interface v2: capabilities, `OAuthConnect`, composer schema, `poll`, `quota`, `delete` | — |
| 16 | OAuth connect flow + token store + refresh job + connection health | 15, 09 (KMS) |
| 17 | Media pipeline: upload API, object storage, scanning, EXIF strip, transcoding worker (KEDA) | 10 (Postgres) |
| 18 | Media delivery: signed URLs on a dedicated, TikTok-verified domain; chunked upload streaming | 17 |
| 19 | Async publish state machine + polling + webhook receiver (CloudEvents) | 15 |
| 20 | Consent records + web composer framework (one-time review links) | 15 |
| 21 | Quota and cost ledger, workspace spend caps, dry-run cost display | 15 |
| 22 | X plug-in (posts, threads, media, delete, edit-window capability) | 15, 16, 18, 21 |
| 23 | Instagram plug-in (Instagram Login, container → publish, carousel, Reels) | 15–21, Meta review |
| 24 | TikTok plug-in (Direct Post, compliant composer, creator_info re-validation) | 15–21, TikTok audit |
| 25 | Store-review prerequisites: privacy policy, terms, data-deletion endpoint, demo accounts | — |

## Open questions

- **TikTok:** will auditors accept *scheduled* (deferred) publishing with consent captured at schedule time? Ask in the audit submission.
- **Instagram:** can one Meta app later move from Instagram Login to Facebook Login (needed for delete), or does that mean a second app and a second review?
- **X:** does the self-reply ("summoned") restriction allow threads to one's own posts? Do refresh tokens rotate? Which per-post price is current?

---
<!-- inkcast-nav -->
[Docs](../../docs/README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
