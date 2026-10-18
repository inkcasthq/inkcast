# E5 Billing, entitlements and metering: design

E5 delivers what the **E10 spike** decides. The design lives in **[E10 Tiers, subscriptions and entitlements](../E10-tiers-and-billing/design.md)**. The decisions are recorded in [ADR-0010](../../adr/0010-pricing-and-packaging.md), [ADR-0011](../../adr/0011-entitlements-and-quotas.md), [ADR-0012](../../adr/0012-offline-licences.md) and [ADR-0013](../../adr/0013-billing-webhooks-and-events.md).

The stories are refined in place in the [E5 backlog](../../backlog/E5-billing.yaml), each marked `refined`, `new` or `replaces`, with acceptance criteria.

| Wave | Features | Depends on |
|---|---|---|
| **A** | F5.1 provider seam, fake provider and `SubscriptionService`; F5.2 catalogue as code; F5.3.S1–S3 counters, `libs/entitlements`, snapshots; F5.4 webhooks; F5.8 offline licences | the E10 spike; runs on SQLite, in parallel with E3 |
| **B** | F5.3.S4–S7 enforcement at use cases, seats, credits, rate limits; F5.6 real provider; F5.7 lifecycle | E3 (organizations, grant API, OpenFGA) |
| **UI** | F5.5 billing UI, paywall, pricing page, downgrade flow | the E4 shell; mockups approved |
| **Later** | F5.9 Agency add-on | E3, E4 |

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)
