# ADR-0010: Pricing, packaging and the billing account

**Status:** accepted as hypotheses, 2026-10-18; prices validated by spike E10.F10.1.S2 · **Design:** [E10](../epics/E10-tiers-and-billing/design.md)

## Decision
- **Five per-seat tiers:** Free, Personal (0), Team (8/seat, 2–20 seats), Business (5/seat, no minimum) and Enterprise (contract). Annual billing gives 2 months free.
- **Fair seats:** writing, publishing and managing are billable. Viewer, reviewer, billing admin and client reviewer are free.
- **Channels** are pooled per organization and scale with seats.
- **Add-ons:** channel packs, credit packs (a credits wallet for AI assists and paid-platform sends such as X), and **Agency** for Business and Enterprise at 5 per client organization.
- **The billing account is the Organization** (its Keycloak Organization id), with one subscription per organization. A managed client organization bills to its agency.
- **Never paywalled** (a public promise, enforced by a catalogue test):
  - REST API, MCP and Git sync;
  - the ledger and duplicate protection;
  - dry run;
  - export and account deletion;
  - token revocation;
  - your own MFA.

## Why
- Per-seat pricing matches the developer tools (GitHub, Vercel) used by Inkcast's Git and MCP audience. It grows with the collaboration features (approvals, SSO, audit), and it doesn't punish the extra channels that E8 adds.
- Free reviewer seats keep approvals cheap to adopt.
- Agency as an add-on, priced per client brand, follows the commercial licence's metric and keeps the pricing page to five tiers.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
