# Keycloak for Inkcast

Keycloak answers "who are you?" for Inkcast. That covers sign-up, social and enterprise sign-in, and MFA. Inkcast keeps its own MCP OAuth, consent and tokens ([ADR-0006](../../docs/adr/0006-identity-on-keycloak.md), epic [E9](../../docs/epics/E9-identity-keycloak/design.md)).

| File | What |
|---|---|
| `realm-inkcast.json` | **The realm as code.** No secret and no person is in it: the client secret and Inkcast's URL come from `${KC_INKCAST_CLIENT_SECRET}` and `${INKCAST_PUBLIC_URL}`, filled from the environment when Keycloak imports the realm at startup. `tests/test_keycloak_realm.py` keeps it that way. On the lab, `scripts/gen-realm-imports.py` renders it into one `KeycloakRealmImport` per environment, `inkcast-staging` and `inkcast-prod` (`deploy/kustomize/shared/keycloak-realms`, #305) |
| `keycloak.yaml`, `kustomization.yaml` | Development and CI only: one Keycloak in `start-dev` mode (in-memory, HTTP), the realm imported at startup, and Mailpit catching its emails (verification, password reset) |

## The realm in short

**Sign-up:** open registration, with the email as the username. A newcomer verifies their email (the link Keycloak sends) and accepts the terms (`TERMS_AND_CONDITIONS`, a default required action) before anything reaches Inkcast. Emails must be verified before an account links by email. The realm declares every required action it relies on, because listing them replaces Keycloak's defaults. SMTP settings are placeholders too (`KC_SMTP_*`).

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

- **Run Keycloak in production mode: the component [`deploy/kustomize/components/keycloak-production`](../kustomize/components/keycloak-production/kustomization.yaml)** (E9.F9.1.S3, #386). It renders the Keycloak operator's `Keycloak` CR (`k8s.keycloak.org`, the CRD both the upstream operator and the Red Hat build of Keycloak Operator serve) in production mode (`start` on PostgreSQL, never `start-dev`), two instances, HTTPS from a `kubernetes.io/tls` Secret `keycloak-tls`, resources, and a CloudNativePG `Cluster` `keycloak-db` ([ADR-0014](../../docs/adr/0014-postgresql-on-cloudnativepg.md)) whose generated Secret `keycloak-db-app` is the only credential, plus the NetworkPolicy that admits the database to Keycloak's pods only. The kustomization that uses it patches `spec.hostname.hostname` with the public URL (the component's own is unresolvable on purpose) and, on plain Kubernetes under Pod Security "restricted", the pod securityContext; the comments in `keycloak.yaml` cite the operator's source for every setting. **Production must also patch the database's `spec.instances` to 3** (ADR-0014: one primary, two replicas); the component's `Cluster` has one instance, the count CI and a small self-host run, as `components/openfga/cluster.yaml`. Proved on kind by `scripts/dev/keycloak-production-e2e.sh` in the kind CI job, with the operators pinned in `scripts/versions.env`: the CR Ready with two pods, the lab's realm imports Done, the realms in PostgreSQL over TLS, discovery over HTTPS, a recreated pod serving them again. The lab's Keycloak (RHBK, hand-made PostgreSQL, realms beyond Inkcast's) is not moved onto it by this story.
- **Import the realm once,** with the same file and placeholders, then manage changes as code: export, review, re-import. A realm that already exists is skipped at startup, so later changes need the `import` command or the admin API. On the lab the operator imports the generated `KeycloakRealmImport`s, the secrets as `spec.placeholders` from a Secret (README, Hosted §2, "The realms as code").
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
