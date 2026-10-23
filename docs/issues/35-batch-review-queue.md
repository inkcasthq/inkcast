# Batch review queue

labels: epic:social,web,mobile

Review a set of posts on one screen (web approvals page F4.9, mobile app issue 13). Per item: media, per-platform preview, every label, scheduled time, quota and cost; approve, edit, reject with a reason; "approve all unchanged" for items with nothing left to choose. Consent stays per post (one record and payload hash each; an edit needs a new approval). TikTok's mandated choices are never batch-filled. Only `can_approve` (F4.9.S2) can decide; the AI assistant can submit (`inkcast_submit_for_review` returns a one-time review link) but never approve. REST: `POST /api/v1/reviews`, `GET /api/v1/reviews/{id}`, `POST /api/v1/reviews/{id}/decisions`. Events `review.requested`, `post.approved`, `post.rejected`; review-latency KPI.

Part of the social epic: docs/design/video-platforms-and-review.md
