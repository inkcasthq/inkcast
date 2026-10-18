# ADR-0012: Offline licences and self-host editions

**Status:** accepted, 2026-10-18 · **Epic:** E5.F5.8

## Decision
- **Self-host never phones home.** Entitlements come from an **Ed25519-signed JWS licence file**, mounted as a Secret or ConfigMap (`INKCAST_LICENCE_FILE`), hot-reloaded and verified with `cryptography` (already a dependency).
- **Keys:** public keys ship in the image by `kid`, with rotation and a revoked-kid list. The signing key never lives in the SaaS: `inkcast licence sign` runs offline, on a hardware-backed key.
- **Expiry:** a warning 30 days ahead, full function through the grace period, and licensed features read-only after it. **Publishing your own content never stops.**
- **Editions:**
  - **Community** (no file): every feature, for your own content.
  - **Agency** licence (`client_orgs: N`): client publishing.
  - **Service-provider** or **OEM**: hosting for others.
  - **Enterprise**: support, LTS, signed images and the air-gap bundle.

  The PolyForm Shield terms are what enforce these rights legally. The file is a compliance aid.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
