# ADR-0011: Entitlements and quotas

**Status:** accepted, 2026-10-18 · **Epics:** E5 (F5.2, F5.3) · **Design:** [E10](../epics/E10-tiers-and-billing/design.md)

## Decision
- **The plan catalogue is code:** `plans.yaml` ships in the image, with typed features, limits and meters in a registry. Plan versions are immutable, and subscriptions pin a version (grandfathering).
- **Per-organization snapshots** combine plan, add-ons and overrides, with a monotonic version and an `entitlements.changed` event. They are read through `libs/entitlements`, with a cache and a last-known-good copy.
- **Never in JWTs, never as OpenFGA tuples**, and never delegated to the payment provider's own entitlement feature.
- **Enforced at the edge of each use case, after authorization:**
  1. OpenFGA: 403 if denied, 503 if the check errors;
  2. business state: 409;
  3. plan: **402** problem+json, with `upgrade_url` and `can_upgrade` or who to ask.
- **Fail-safe classes when plan data is missing or stale:**
  - safety controls stay on;
  - accepted work is honoured;
  - cost-bearing features are denied;
  - capacity falls back to Free limits;
  - premium features fall back to Free;
  - API, MCP, Git and export stay on.
- **Quotas:**
  - Resource counts are checked inside the owning service's transaction.
  - Meters use atomic `INSERT … ON CONFLICT … WHERE used + n <= limit` counters with idempotent usage events, in the owning service's schema.
  - Credits and extra capacity are prepaid packs, with no post-paid usage billing at launch. A `MeterSink` seam exists for later.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
