# Platform: Facebook Pages plug-in

labels: epic:social,platform

Reels on Facebook Pages through the same Meta app and Business Verification as Instagram (issue 23). Permissions `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`. `video_reels` upload session (resumable by offset, or a public `file_url` pull from the media domain), then publish. 9:16, 3–90 s. 30 API-published Reels per rolling 24 h per Page, enforced by the quota ledger. `is_ai_generated` as a composer field, never set silently. Inkcast's scheduler, not Facebook's native scheduling. Photo and text Page posts later.

Part of the social epic: docs/design/video-platforms-and-review.md (extends docs/design/social-platforms-epic.md)
