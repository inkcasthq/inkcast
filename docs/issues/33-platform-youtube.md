# Platform: YouTube plug-in

labels: epic:social,platform

Google `OAuthConnect` (PKCE, offline access; scopes `youtube.upload` + `youtube.readonly`), resumable upload streamed from object storage, async state machine through YouTube's processing, permalink after publish. Composer fields with no defaults: audience (`status.selfDeclaredMadeForKids`), visibility (`status.privacyStatus`), and altered or synthetic content (`status.containsSyntheticMedia`, required when the media is realistic AI-generated or altered). Title, description, tags, thumbnail, playlist. Quota ledger per Google Cloud project (an upload is ~100 units since 2025-12-04; `videos.insert` has its own bucket since 2026-06-01). Delete and metadata edit supported. Needs Google OAuth app verification; plan the YouTube API compliance audit, and confirm whether uploads from unverified projects are still private.

Part of the social epic: docs/design/video-platforms-and-review.md (extends docs/design/social-platforms-epic.md)
