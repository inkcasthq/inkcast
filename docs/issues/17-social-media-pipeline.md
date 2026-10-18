# Social epic: media upload, storage and transcoding

labels: epic:social,media

Upload API (base64 / pre-signed), S3-compatible storage (cloud, or Rook-Ceph RGW / MinIO on-prem), content addressing, malware scan hook, EXIF/GPS stripping, per-platform derivatives (IG JPEG 4:5–1.91:1, X ≤ 5 MB, TikTok ≤ 1080p, video re-encode), KEDA-scaled worker.

**Done when:** derivatives validated against each platform's rules in tests; originals deleted after retention.

Part of the X / Instagram / TikTok epic: docs/design/social-platforms-epic.md
