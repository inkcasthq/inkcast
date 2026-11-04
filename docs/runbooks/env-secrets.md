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

1. **Restore `inkcast-secrets` from its copy.** The cluster backup keeps one ([ADR-0020](../adr/0020-root-secret-copy-in-its-own-storage-location.md)): the OADP Schedule `inkcast-<side>-root-secret` in `openshift-adp` (`deploy/kustomize/shared/oadp`) copies, every day at 03:47 UTC, the Secrets of `$NS` that carry the label `inkcast.io/root-secret: "true"` (`inkcast env-secrets` sets it on `inkcast-secrets`; nothing else carries it) into the storage location `root-secrets`, a bucket of its own, apart from wherever `inkcast-backups` is copied, and keeps each copy 90 days. On OpenShift Local that location is an in-cluster store (`deploy/kustomize/shared/seaweedfs`, the stand-in for production's off-cluster bucket encrypted with a KMS key), so this copy survives a deleted Secret but not the loss of the whole cluster: the store is in the same cluster as the data. The latest copy:

   ```bash
   oc -n openshift-adp get backups.velero.io -l velero.io/schedule-name=inkcast-<side>-root-secret --sort-by=.status.completionTimestamp \
     -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,ITEMS:.status.progress.itemsBackedUp,COMPLETED:.status.completionTimestamp,EXPIRES:.status.expiration
   ```

   Take the newest `Completed` one with `ITEMS` 1 (`<latest>` below), and restore it, limited to `secrets` carrying the label in `$NS`. Velero "will never overwrite data that already exists in your cluster" (`existingResourcePolicy: none`), so a Secret a pair still holds, or one made meanwhile, is left as it is:

   ```bash
   oc -n openshift-adp create -f - <<EOF
   apiVersion: velero.io/v1
   kind: Restore
   metadata:
     name: <latest>-restore
   spec:
     backupName: <latest>
     includedNamespaces: [$NS]
     includedResources: [secrets]
     labelSelector: {matchLabels: {inkcast.io/root-secret: "true"}}
     existingResourcePolicy: none
     includeClusterResources: false
     restorePVs: false
   EOF
   oc -n openshift-adp get restores.velero.io <latest>-restore -o jsonpath='{.status.phase} errors={.status.errors}{"\n"}'   # Completed errors=
   ```

   The kinds are written in full (`backups.velero.io`, `restores.velero.io`): on the lab `oc get backup` resolves to CloudNativePG's `backups.postgresql.cnpg.io` first, and answers "not found" for a Velero backup.

   ```bash
   ```

   With the `velero` command instead: `velero restore create --from-backup <latest> --include-resources secrets --selector inkcast.io/root-secret=true --include-namespaces $NS -n openshift-adp`. The value is the one the pods hold, so nothing restarts; the restored Secret carries the label, so the next copy holds it. Then step 3: the CronJob's next run (or the Job there) exits 0 and says every Secret is in agreement, and a sealed credential still reads. `scripts/ops/root-secret-drill.sh $NS` does all of it in order (a safety copy on your host, mode 600, removed at the end; a backup; the deletion; the restore; the comparison of every value by SHA-256; the run; every stored credential unsealed inside the server's pod with the key it mounts): run it on staging after any change to the copy, and never on an environment whose copy you have not seen `Completed`. A copy kept by hand (`oc get secret ... -o yaml`, somewhere safe) is put back with `oc -n $NS apply -f <the file>`; the Schedule copies no other Secret: `inkcast-db/...` and `keycloak/...` are pairs, which the CronJob copies from the side that survives, and a pair lost on both sides is step 2.

2. **No copy: make new values, and accept what that costs.** The init container makes them: `oc -n $NS rollout restart deploy/openfga` (the CronJob never will). Then, per value:
   - **the role's password**: nothing more. CloudNativePG sets the new password on the role as soon as the Secret is written (`cnpg.io/reload`), and the restarted pod's OpenFGA connects with it.
   - **`fga-api-token`**: nothing more. The restarted OpenFGA serves with the new key, and Inkcast re-reads it from the mounted file (`INKCAST_FGA_API_TOKEN_FILE`; [secrets](../reference/secrets.md), "the file is re-read").
   - **`secret-key`**: every platform credential sealed with the old key is unreadable for good. Restart Inkcast's pod as well (`oc -n $NS rollout restart deploy/inkcast`), so every process runs on the new key from its start, and have each platform connected again by its owner. The SQLite backups taken under the old key hold credentials nobody can read either.
   - **a Keycloak client secret**: the init container makes a new value in both Secrets, but Keycloak's realm keeps the one it was imported with (`kc.sh import --override=false`, README "The realms as code"). Set the same value on the client in Keycloak (the admin console, realm `inkcast-<side>`, client `inkcast-identity` or `inkcast-sync`, Credentials), or set a secret there first and put it in both Secrets by hand; the sign-in, or the user sync, fails until the three agree.

3. **Check:** `oc -n $NS create job --from=cronjob/inkcast-env-secrets env-secrets-check`, then `oc -n $NS logs job/env-secrets-check` ends with `<namespace>: every generated Secret is present and in agreement.` and the Job is `Complete`.

## Why two modes

The init container (no flag) makes every absent value: that is the first install, before anything runs on a value. The CronJob (`--repair-only`) only copies: by the time it runs, something is running on every value, and a value made anew is a value lost to it. Both are the same code, `src/inkcast/env_secrets.py`, with one switch; `tests/test_env_secrets.py` (`RepairOnly`) holds each case, and `scripts/dev/env-secrets-e2e.sh` (proofs 6 and 7) runs both on kind in CI.
