# Keycloak for Inkcast

Keycloak answers "who are you?" for Inkcast. That covers sign-up, social and enterprise sign-in, and MFA. Inkcast keeps its own MCP OAuth, consent and tokens ([ADR-0006](../../docs/adr/0006-identity-on-keycloak.md), epic [E9](../../docs/epics/E9-identity-keycloak/design.md)).

| File | What |
|---|---|
| `realm-inkcast.json` | **The realm as code.** No secret and no person is in it: the client secret and Inkcast's URL come from `${KC_INKCAST_CLIENT_SECRET}` and `${INKCAST_PUBLIC_URL}`, filled from the environment when Keycloak imports the realm at startup. `tests/test_keycloak_realm.py` keeps it that way |
| `keycloak.yaml`, `kustomization.yaml` | Development and CI only: one Keycloak in `start-dev` mode (in-memory, HTTP), the realm imported at startup |

## The realm in short

**Sign-up:** open registration, with the email as the username. Emails must be verified before an account links by email.

**Account protection:**
- brute-force protection;
- passwords of 12 characters or more that aren't the username or email;
- 5-minute access tokens;
- refresh tokens revoked on use.

**Roles:**
- `inkcast-user` is in every account's default roles, and Inkcast requires it (`OIDC_REQUIRED_ROLE`). Removing it from a person stops them signing in to Inkcast.
- `inkcast-admin` is for operators; MFA is required (E9.F9.4).

**Client `inkcast-identity`:**
- confidential, authorization code flow with PKCE S256;
- the exact redirect `<INKCAST_PUBLIC_URL>/auth/callback`;
- no password grant, so people always sign in on Keycloak's own page.

## Try it (kind)

```bash
scripts/dev/keycloak-e2e.sh      # needs a kind cluster with inkcast:ci loaded; the kind CI job runs it
```

The script:
1. deploys Keycloak with the realm;
2. checks that the placeholders were filled;
3. creates two people at runtime (random passwords), one with `inkcast-user` and one with it removed;
4. deploys Inkcast from the `ci-oidc` overlay;
5. runs `scripts/dev/oidc_signin.py` inside the Inkcast pod. The first person must sign in end to end and see themselves at `/api/v1/me`; the second must be refused by Inkcast after Keycloak accepted their password.

## Production

Don't use `keycloak.yaml` in production. Instead:

- **Run Keycloak in production mode.** Use `start --optimized` on PostgreSQL, preferably a CloudNativePG `Cluster` ([ADR-0014](../../docs/adr/0014-postgresql-on-cloudnativepg.md)). Run two or more replicas behind TLS, with `KC_HOSTNAME` set to the public URL. On OpenShift, the Red Hat build of Keycloak Operator manages all of this.
- **Import the realm once,** with the same file and placeholders, then manage changes as code: export, review, re-import. A realm that already exists is skipped at startup, so later changes need the `import` command or the admin API.
- **Keep the secrets out of Git.**
  - `KC_INKCAST_CLIENT_SECRET` and the identity-provider secrets come from Kubernetes Secrets, through the secrets design.
  - Inkcast reads the same client secret as `OIDC_CLIENT_SECRET_FILE`.
- **Point Inkcast at the realm:** `INKCAST_LOGIN=oidc`, `OIDC_ISSUER=https://<keycloak>/realms/inkcast`, `OIDC_CLIENT_ID=inkcast-identity`, `OIDC_REQUIRED_ROLE=inkcast-user`.
- **If it fails:**
  - Existing MCP tokens and PATs keep working while Keycloak is down; only new sign-ins fail.
  - Realm upgrades and signing-key rotation are E9.F9.7.
  - Back up Keycloak's database with the same operator (the same backups as Inkcast's PostgreSQL).

---
<!-- inkcast-nav -->
[Docs](../../docs/README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
