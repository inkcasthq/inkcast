# Social epic: async publish state machine, polling and webhooks

labels: epic:social,scheduler

States `preparing → processing → published | failed | unknown`; per-platform poll backoff (X check_after_secs, IG ≤ 1/min for 5 min, TikTok ≤ 30/min); signed webhook receiver normalised to CloudEvents; permalink fetch after publish.

Part of the X / Instagram / TikTok epic: docs/design/social-platforms-epic.md
