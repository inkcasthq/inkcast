# Platform: Ghost plug-in

labels: platform

Ghost Admin API with an Admin API key (`id:secret`), short-lived JWT per request (HS256, `aud: /admin/`).

- posts via `?source=html` (convert markdown), tags, `canonical_url`, `custom_excerpt` = subtitle, `feature_image`
- `status: draft|published`, optional newsletter send (explicit opt-in field)

**Done when:** fake Ghost API + tests; JWT signing unit-tested.
