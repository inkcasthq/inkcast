# ADR-0009: Identifiers in authorization tuples

**Status:** accepted, 2026-10-18 · **Epics:** E3, E9

## Decision
- **Users:** `user:<inkcast user id>` (`usr_…`), never an email or username.
  - The Inkcast id maps 1:1 to the identity key `oidc:<issuer>#<sub>`, so a Keycloak `sub` resolves to exactly one user.
  - Sign-in through GitHub or a password works the same way.
  - Changing identity provider or issuer doesn't rewrite tuples.
- **Organizations:** `organization:<keycloak organization id>`. Each personal account has its own organization.
- **Workspaces, channels, posts:** `workspace:<id>`, `channel:<id>`, `post:<id>` (Inkcast ids). Never names or slugs, which can change.
- **Custom roles:** `role:<id>`, with `org` linking each role to its organization.
- Ids are opaque and contain no personal data, so logs of denied checks reveal nothing beyond ids.

## Why not the raw `sub`
It ties authorization to one issuer. A realm migration or a second sign-in method would create a second identity for the same person.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
