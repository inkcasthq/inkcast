# ADR-0013: Billing webhooks, events and card data

**Status:** accepted, 2026-10-18 · **Epic:** E5.F5.4 (provider: the bake-off ADR from E10.F10.2.S4)

## Decision
- **Hosted checkout and portal only.** Inkcast never touches card data (PCI SAQ A at most), and the SPA's CSP stays strict.
- **Signed intake:**
  - verify the HMAC over the **raw body** before parsing, in constant time;
  - enforce a timestamp tolerance;
  - cap the body at 64 KB and rate-limit the endpoint;
  - read the secret through `_FILE` as a `SecretValue`.
- **Inbox:** a unique `(provider, provider_event_id)`, a fast 200, then a worker that **re-fetches the subscription from the provider's API**. The API is the source of truth, so out-of-order and duplicate events converge.
- **Outbox:** a single transaction updates the subscription, recomputes the snapshot and appends `subscription.changed` / `entitlements.changed`. Other services poll the outbox (no broker on day one).
- **Daily reconciliation** with the provider raises a drift alert.
- **Provider calls** take idempotency keys. Provider SDK types never leave the `BillingProvider` adapter.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
