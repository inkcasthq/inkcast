# Diagrams

Each diagram has its own page, `docs/diagrams/<slug>/source.html`, made to the [diagram-kit](https://github.com/ephico2real2/diagram-kit) `/visual` standard. The page is the source; the PNG beside it is rendered from it and never edited by hand.

**Inkcast diagrams are light theme only.** The pages carry no dark tokens, and only `<name>.light.png` is committed. Documents embed the PNG as a plain `<img>`.

| Diagram | Shows | Used in |
|---|---|---|
| [who-answers-what](who-answers-what/source.html) | Keycloak, OpenFGA and the Inkcast DB, one question each | [E3](../epics/E3-tenancy-authorization/design.md), [authorization](../authorization.md) |
| [check-path](check-path/source.html) | token → scope → OpenFGA → state → plan, and each refusal code | [E3](../epics/E3-tenancy-authorization/design.md), [E10](../epics/E10-tiers-and-billing/design.md) |
| [checkout-entitlement](checkout-entitlement/source.html) | hosted checkout → signed webhook → re-read → one transaction | [E10](../epics/E10-tiers-and-billing/design.md) |
| [tenancy-agency](tenancy-agency/source.html) | Keycloak Organizations, workspaces, one-tuple agency grants | [E10](../epics/E10-tiers-and-billing/design.md) |
| [subscription-states](subscription-states/source.html) | trialing → active → past due → restricted → canceled → Free | [E10](../epics/E10-tiers-and-billing/design.md) |

## Change a diagram

1. Edit `source.html`. Solid box = shipped, dashed box = proposed; a dashed arrow is a relationship and needs a label.
2. Check and render (needs `pip install git+https://github.com/ephico2real2/diagram-kit`):

   ```bash
   diagram-render --check docs/diagrams/<slug>/source.html
   diagram-render docs/diagrams/<slug>/source.html docs/diagrams/<slug> <slug>
   rm docs/diagrams/<slug>/<slug>.dark.png    # light only
   ```

3. Commit the page and the light PNG together.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
