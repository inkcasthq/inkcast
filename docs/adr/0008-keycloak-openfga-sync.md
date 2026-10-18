# ADR-0008: Keeping OpenFGA in step with Keycloak

**Status:** proposed, 2026-10-18; decided by spike E3.F3.0.S2 · **Epics:** E9.F9.5, E3.F3.8

## Context
Organization membership and org admins are managed in Keycloak (Organizations). OpenFGA must reflect them, or removed people keep access.

## Options
1. **An event-listener SPI in Keycloak** (for example `keycloak-openfga-event-publisher`) pushes changes as they happen. It is fast, but it is a custom JAR in the Keycloak image, which has to be rebuilt and mirrored for air-gapped installs.
2. **Inkcast polls the Keycloak admin-events API.** No Keycloak customisation, at the cost of seconds of delay.
3. **Either of the above, plus a scheduled reconcile.**

## Decision (default unless the spike finds otherwise)
**Option 3, with polling (2) as the event source.**

- An `IdentitySync` worker (E9.F9.5) consumes admin and Organization events idempotently and writes tuples through the transactional outbox.
- A scheduled reconcile compares Keycloak membership with the OpenFGA tuples and fixes drift. **A removed user loses access even if an event was missed.**
- Drift is a metric and an alert.
- Workspace-level grants are written only by the Inkcast API, never by Keycloak.

## Consequences
- No custom Keycloak build, which keeps air-gapped operations simple.
- Propagation takes seconds rather than instantly, so disabling a user also revokes their Inkcast tokens immediately (E9.F9.5.S2).

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
