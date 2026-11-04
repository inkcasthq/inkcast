# The generated Secrets: a lost copy, a lost value

Each lab environment's Secrets are made by the cluster (`inkcast env-secrets`, `deploy/kustomize/components/env-secrets`, #323): the init container `env-secrets` of OpenFGA's pod makes every absent value at each start of that pod, and the CronJob `inkcast-env-secrets` runs the same command every ten minutes with `--repair-only` (#342, the owner's decision on PR #423). This runbook is for the two cases the CronJob tells apart.

**Namespace.** The commands below write the environment's namespace as `$NS`: `NS=inkcast-staging` for `deploy/kustomize/environments/staging`, `NS=inkcast-prod` for `environments/production`. The side is `staging` or `prod`. A self-host from the overlays (`NS=inkcast`) brings its own Secrets and has neither the init container nor the CronJob: nothing here applies to it.

What is held where:

| Value | Held in | A pair? |
|---|---|---|
| the database role's password | `inkcast-db/inkcast-pg-openfga-<side>` (what CloudNativePG sets on the role) and `$NS/openfga-db-app` (what OpenFGA connects with; its `uri` carries it) | yes |
| the identity client's secret | `keycloak/inkcast-<side>-realm` `identity-client-secret` and `$NS/inkcast-secrets` `oidc-client-secret` | yes |
| the user sync's client secret | `keycloak/inkcast-<side>-realm` `sync-client-secret` and `$NS/inkcast-secrets` `keycloak-sync-secret` | yes |
| `secret-key` (seals the platform credentials at rest) and `fga-api-token` (OpenFGA's preshared key) | `$NS/inkcast-secrets` only | no: a root value |

## A copy is lost: nothing to do

One side of a pair deleted (`inkcast-db/inkcast-pg-openfga-<side>`, say, or `keycloak/inkcast-<side>-realm`): the CronJob's next run copies it back from the side that survives, within ten minutes, with no restart and no apply. The value is the same, so OpenFGA, Keycloak and Inkcast keep running on it. To see it: `oc -n $NS get cronjob,jobs` (the last successful Job is kept), `oc -n $NS logs job/<name>` (it names Secrets and keys, never a value). To hurry it: `oc -n $NS create job --from=cronjob/inkcast-env-secrets env-secrets-now`; the controller removes that Job with the history it keeps.

## A root value is lost: the run fails and says so

A value absent on every side (`inkcast-secrets` deleted, so `secret-key` and `fga-api-token` are gone; both sides of a pair deleted) is **never made anew by the CronJob**: in a running environment a new value cuts the pods off what they hold. A new `secret-key` makes every sealed platform credential unreadable; a new `fga-api-token` is refused by the running OpenFGA until it restarts; a new role password is set on the role at once, and OpenFGA's next connections fail; Keycloak's realm keeps the client secret it was imported with, and the sign-in fails. So the run exits 1, the Job shows `Failed` in `oc -n $NS get jobs`, and its log (`oc -n $NS logs job/<name>`; the last three failed Jobs are kept) names what is lost and this runbook (the message names the namespace; it is written `$NS` here):

```
inkcast env-secrets: $NS/inkcast-secrets is absent: secret-key and fga-api-token are held nowhere else and lost. This run makes no new value (...). Restore the Secret from a copy; without one, `oc -n $NS rollout restart deploy/openfga` makes new values and every platform must be connected again (docs/runbooks/env-secrets.md).
inkcast env-secrets: Nothing was made anew (--repair-only): what one side still held was copied.
```

The pairs that can be copied are copied in that same run; a Secret that would be created with a lost value (an `inkcast-secrets` holding only the two client secrets the realm still has) is left absent, never half-made, so a restore brings it back whole. Then:

1. **A copy of the Secret exists** (an `oc get secret ... -o yaml` kept somewhere safe; a cluster backup, such as OADP on OpenShift). Put it back: `oc -n $NS apply -f <the file>` (for `inkcast-db/...` or `keycloak/...`, that namespace). The value is the one the pods hold, so nothing restarts; the CronJob's next run copies any key a pair still lacks and exits 0. The repository makes no such copy today: `inkcast backup` copies the SQLite file, and that file is useless without `secret-key` (the backup component says so), so keep a copy of `inkcast-secrets` where the backups' key is kept.

2. **No copy: make new values, and accept what that costs.** The init container makes them: `oc -n $NS rollout restart deploy/openfga` (the CronJob never will). Then, per value:
   - **the role's password**: nothing more. CloudNativePG sets the new password on the role as soon as the Secret is written (`cnpg.io/reload`), and the restarted pod's OpenFGA connects with it.
   - **`fga-api-token`**: nothing more. The restarted OpenFGA serves with the new key, and Inkcast re-reads it from the mounted file (`INKCAST_FGA_API_TOKEN_FILE`; [secrets](../reference/secrets.md), "the file is re-read").
   - **`secret-key`**: every platform credential sealed with the old key is unreadable for good. Restart Inkcast's pod as well (`oc -n $NS rollout restart deploy/inkcast`), so every process runs on the new key from its start, and have each platform connected again by its owner. The SQLite backups taken under the old key hold credentials nobody can read either.
   - **a Keycloak client secret**: the init container makes a new value in both Secrets, but Keycloak's realm keeps the one it was imported with (`kc.sh import --override=false`, README "The realms as code"). Set the same value on the client in Keycloak (the admin console, realm `inkcast-<side>`, client `inkcast-identity` or `inkcast-sync`, Credentials), or set a secret there first and put it in both Secrets by hand; the sign-in, or the user sync, fails until the three agree.

3. **Check:** `oc -n $NS create job --from=cronjob/inkcast-env-secrets env-secrets-check`, then `oc -n $NS logs job/env-secrets-check` ends with `<namespace>: every generated Secret is present and in agreement.` and the Job is `Complete`.

## Why two modes

The init container (no flag) makes every absent value: that is the first install, before anything runs on a value. The CronJob (`--repair-only`) only copies: by the time it runs, something is running on every value, and a value made anew is a value lost to it. Both are the same code, `src/inkcast/env_secrets.py`, with one switch; `tests/test_env_secrets.py` (`RepairOnly`) holds each case, and `scripts/dev/env-secrets-e2e.sh` (proofs 6 and 7) runs both on kind in CI.
