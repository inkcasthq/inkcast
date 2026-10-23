# Runbooks: what to do when an Inkcast alert fires

Every alert in `src/inkcast/libs/alerting.py` links here by its name (a test checks the anchors). Each section says what the alert means, how to confirm it, what to do first, and when to escalate. The generated rules are in `deploy/kustomize/components/prometheus-rules/`; regenerate with `python scripts/gen-alerts.py`.

**First five minutes, for any page:** `oc get pods -n inkcast` (or `kubectl`), `oc logs deploy/inkcast-<service> --since=15m | grep '"level":"error"\|"level":"critical"'`, then `curl -s :9090/readyz` on the pod. Log events are catalogued in `libs/observability.EVENTS`; the `event` field is what to grep.

## Severities and routing

| `severity` | Means | Alertmanager route |
|---|---|---|
| `critical` | people are losing scheduled posts or cannot use the product; page now | `team=inkcast, severity=critical` → pager (PagerDuty/Opsgenie or an on-call Slack channel with `repeat_interval: 1h`) |
| `warning` | something will become critical or is degrading quality; next business hours | `team=inkcast, severity=warning` → the ops channel, grouped by `service`, `repeat_interval: 12h` |
| `info` | a signal for dashboards and capacity, no action | `team=inkcast, severity=info` → a low-noise channel or no receiver |

SLO burn-rate alerts carry `slo`, `burn_rate`, `long_window` and `short_window` labels; group by `slo` so one bad hour yields one notification.

## InkcastTargetDown

**Meaning:** Prometheus cannot scrape a pod's metrics port. The pod is down, not ready, or the `inkcast-metrics` NetworkPolicy no longer admits Prometheus (its namespace label, or its pods' `app.kubernetes.io/name: prometheus` label).

1. `oc get pods -l app.kubernetes.io/part-of=inkcast`; a pod not `Running`/`Ready` is the cause.
2. If pods look fine: `oc get networkpolicy inkcast-metrics -o yaml` and check both selectors against the Prometheus pods (`oc get pods -n openshift-user-workload-monitoring --show-labels`); `oc get servicemonitor inkcast` must name port `metrics`.
3. Exit code 78 in the previous container log is a configuration problem: the `config.unsafe` log line lists it.

## InkcastSchedulerStalled

**Meaning:** no scheduler tick in 10 minutes. Scheduled posts stay in the queue.

1. `oc get pod -l inkcast.io/service=scheduler-worker` (in `All` mode, the single pod). Readiness reports `scheduler: no tick recently`.
2. Look for `scheduler.tick.failed` in its log: a crashing tick retries every interval, and the traceback says why (usually storage).
3. Restart the pod if it is wedged; a tick in progress finishes first (SIGTERM is honoured), so nothing is half-sent.
4. After recovery, posts interrupted mid-send are resolved by `publish.recovered`; check for `unconfirmed` ones (next runbook).

## InkcastPostsOverdue

**Meaning:** posts are past due by more than `INKCAST_OVERDUE_GRACE` (600 s). The scheduler is stopped, behind, or every tick fails.

1. Is `InkcastSchedulerStalled` also firing? Follow it first.
2. `inkcast_posts_oldest_due_age_seconds` and `inkcast_posts_due` on the dashboard: a growing queue with ticks running means the batch (20 per tick) is too small for the burst, or a platform is slow (`inkcast_publish_duration_seconds`). Scale the scheduler (E3: several replicas are safe; the claim is atomic) or lower `INKCAST_SCHEDULER_INTERVAL`.
3. Posts that go out late are still published once; customers see the lag in their on-time rate.

## InkcastQueueHeadLagHigh

**Meaning:** the oldest due post has waited over 5 minutes but is not yet overdue. Early warning for `InkcastPostsOverdue`; same checks, no page.

## InkcastUnconfirmedPublishes

**Meaning:** a publish ended `unconfirmed`: the request was sent and the platform's answer was lost (timeout or 5xx after the send), or the platform plug-in crashed mid-send (`internal error` in the post's `error`: a plug-in bug, roll back the image). Inkcast never retries these blindly.

1. Find them: the admin portal (E6) or `SELECT id, user_id, platform, title FROM posts WHERE status='unconfirmed'`.
2. Check the platform for the post. If it exists, record it (`result_url`, `remote_id`, status `published`); if not, set `scheduled` with `run_at=now`. Git-published posts resolve themselves on the next sync (`find_existing`).
3. A burst of unconfirmed posts on one platform means that platform is timing out: see `InkcastPublishFailureRateHigh`.

## InkcastPublishFailureRateHigh

**Meaning:** more than 20% of attempts to one platform failed in 30 minutes (with at least 5 attempts).

1. The error text is in the posts' `error` column and in the `publish.*` log lines; 401/403 means revoked credentials (customers must reconnect; E7 channel health will tell them), 5xx or timeouts mean a platform outage (a plug-in crash ends `unconfirmed`: see `InkcastUnconfirmedPublishes`).
2. Check the platform's status page; nothing to do for an outage except watch retries (`INKCAST_PUBLISH_MAX_ATTEMPTS` sends, `INKCAST_PUBLISH_RETRY_DELAYS` apart: by default 4 sends, 60 s, 5 m and 30 m apart) and unconfirmed posts.

## InkcastHttp5xxRateHigh

**Meaning:** over 5% of requests on one route return 5xx for 10 minutes.

1. Logs with `"status":5` on that route; the traceback is in the same line (`exc`).
2. Storage errors (`database is locked`, disk full) point at `InkcastDataVolumeNearlyFull` or a stuck writer; platform errors on `/api/v1/platforms/*/destinations` are the platform's.
3. Roll back if the rate started with a deploy.

## InkcastGatewayUpstreamUnreachable

**Meaning:** the built-in Gateway got a connection error to a service (clients see 502 `upstream_unreachable`). Only in split mode without Gateway API.

1. `oc get endpoints inkcast-<service>`: no addresses means the pod is not ready.
2. `oc get networkpolicy inkcast-internal`: the Gateway must reach port 8787 of the services.
3. `INKCAST_UPSTREAM_<SERVICE>` values in the Gateway Deployment match the Service names.

## InkcastOutboxConsumerLag

**Meaning:** domain events are not being folded into KPIs; customer insights are stale (nothing else is affected).

1. InsightsService pod and its readiness (`outbox consumer: no tick recently`).
2. `outbox.consumer.failed` log lines name the sink and the batch; a bug in a rollup rule fails the same batch forever. Fix forward; replays are safe (idempotent by event id).
3. `inkcast_outbox_pending` tells how far behind; the consumer catches up at 200 events per pass.

## InkcastCacheBackendErrors

**Meaning:** Redis/Valkey is unreachable or refusing. Every service keeps working on in-process fallbacks: rate limits are per replica, and (later) locks and sessions are not shared.

1. `oc get pods -n <cache namespace>`; the Valkey/Redis logs; `AUTH` failures mean `INKCAST_CACHE_PASSWORD` rotated without a rollout.
2. Network: the egress policy must allow port 6379 to the cache.
3. The first failure logs `cache.backend.unavailable` once per process.

## InkcastRateLimitSurge

**Meaning:** sustained 429s on unauthenticated writes. Usually a client in a loop (a misconfigured CI job hitting `/register`), sometimes abuse.

1. `inkcast_ratelimit_total` by `limit` says which endpoint; the access log (`http.request`, status 429) says which client address.
2. Abuse: add the address to the edge (Gateway API policy / ingress) and keep the application limit as the backstop.

## InkcastPodRestarting

**Meaning:** a container restarted more than 3 times in an hour.

1. `oc describe pod` for the last state: OOMKilled (raise the memory limit; the default is 256Mi), exit 78 (configuration; the log says what), or a failing liveness probe (the process is wedged; the metrics port must answer `/livez`).

## InkcastDataVolumeNearlyFull

**Meaning:** the SQLite volume has under 10% free.

1. Grow the PVC (most storage classes allow online expansion) or purge: `purge_expired` runs every 90 ticks and drops expired tokens, tickets, outbox rows older than `INKCAST_OUTBOX_RETENTION_DAYS` (7) that every consumer has passed, and hourly KPI rows older than `INKCAST_KPI_HOURLY_RETENTION_DAYS` (90); shortening either frees space at the next run. A consumer behind holds every outbox row it hasn't passed, whatever the setting: see InkcastOutboxConsumerLag first.
2. With PostgreSQL (E3) this alert belongs to the database operator.

## InkcastBackupMissing

**Meaning:** the `inkcast-backup` CronJob (the `backup` component) has not succeeded for more than 26 hours, so a lost volume would lose more than a day.

1. `oc get cronjob inkcast-backup` and `oc get jobs -l app.kubernetes.io/component=backup`; the failed Job's log has a `backup.failed` line with the reason.
2. Common reasons: the backup volume is full (lower `--keep` or grow `inkcast-backups`), or the Job could not schedule next to the Inkcast pod (both mount the ReadWriteOnce data volume, so they must share a node).
3. Take one now: `oc create job inkcast-backup-now --from=cronjob/inkcast-backup`. Restores: [backup and restore runbook](backup-restore.md).
4. Needs kube-state-metrics (OpenShift ships it). With PostgreSQL (E3) this alert moves to the database operator's backups.

## InkcastIdentitySyncFailing

**Meaning:** every pass of the Keycloak user sync (E9.F9.5.S2) failed for 15 minutes. People disabled or deleted in Keycloak can't sign in again, but the OAuth tokens and personal access tokens Inkcast already gave them keep working until the sync recovers or the tokens expire. Sign-in is not affected: readiness deliberately ignores the sync.

1. The IdentityService log has `identity.sync.failed` lines with the reason.
2. "Keycloak refused the sync service account (HTTP 401)": the `inkcast-sync` client secret in Keycloak and `INKCAST_KEYCLOAK_SYNC_SECRET` differ (a rotation done on one side only). Set both to the same value.
3. "HTTP 403" on `admin-events` or `events`, or "the reconcile could act on nobody" with an `identity.sync.person.failed` line (`status` 403) for every person: the service account lost `view-users` or `view-events` (realm-management). The realm file grants both.
4. Connection errors: Keycloak is down, or a NetworkPolicy blocks the admin API (same host and port as the issuer).
5. Once it recovers, the next full reconcile (every `INKCAST_KEYCLOAK_RECONCILE_INTERVAL`, 10 minutes by default, and at start) catches anyone missed. To force it now, restart IdentityService.
6. A self-deletion whose `DELETE_ACCOUNT` event Keycloak trimmed meanwhile (the realm keeps events 7 days) is still cut off by that reconcile, and erased `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` (30) later, like an administrator's deletion. An outage longer than the retention therefore delays those erasures by the grace period; it never loses them.

## InkcastIdentitySyncPersonFailing

**Meaning:** for an hour, every 30 minutes has had a Keycloak user sync failure at one stage (`stage="person"`: Keycloak answered an error about one person, a 500 for that user or a malformed id, on `users/{id}` or their `DELETE_ACCOUNT` events). The pass went on past them (#103), so everyone else is cut off and erased as usual. For that person: if their `users/{id}` read fails (`call=user`), nothing is done until Keycloak answers about them, so their tokens keep working if Keycloak has disabled or deleted them meanwhile; if Keycloak answered that they are gone but the read of their `DELETE_ACCOUNT` events fails (`call=events`), they are cut off at once and only the erasure decision waits. The sync as a whole is fine; if `InkcastIdentitySyncFailing` fires too, follow it first (a realm-wide 403 or 500 fails everyone, which also fails the reconcile as a pass).

1. The IdentityService log has `identity.sync.person.failed` lines: `user_id` (the Inkcast id; `null` for someone Inkcast does not know), `via` (`event` or `reconcile`), `call` (`user` for `users/{id}`, `events` for their `DELETE_ACCOUNT` events) and the HTTP `status`. Nothing from Keycloak's answer is logged. The Keycloak id is the `identity` column of that user (`oidc:<issuer>#<id>`).
2. The person is read again at every reconcile (every `INKCAST_KEYCLOAK_RECONCILE_INTERVAL`, 10 minutes by default), and at every pass (30 s) while a `DELETE_ACCOUNT` event of theirs holds the cursor; the alert clears about 30 minutes after the last failure (it looks at the last 30 minutes, with no hold).
3. The fault is in Keycloak, about that one user: open them in its admin console (Users, search by the id), which makes the same `users/{id}` read, and Keycloak's own log says why it fails (`status` 500) or refuses the id (400). Fix or delete the user there: a deletion answers 404 at the next read, and the sync cuts them off and erases them after the grace period.
4. A `DELETE_ACCOUNT` event of that person holds the user-events cursor (`sync_state`, `keycloak-user-events`), so every pass re-reads the events since it; that is by design (an erasure is never passed over) and costs one read per pass until they are erased. If the person is gone and that event is all that is left, nothing needs doing: the first read that answers 404 erases them.

## InkcastIdentitySyncBreakerOpen

**Meaning:** a full reconcile of the Keycloak user sync found at least `INKCAST_KEYCLOAK_BREAKER_MIN` (5) people, and more than `INKCAST_KEYCLOAK_BREAKER_PERCENT` (10) of the people it read, gone from Keycloak at once (404 `User not found`, no `DELETE_ACCOUNT` event of theirs), and the circuit breaker tripped (E9.F9.5.S7, issue #105). Nobody was cut off or erased, no erasure grace period started or advanced, and it stays that way, for everyone Keycloak says is gone, until an operator decides. Keycloak answers 404 about people who still exist when it was restored from an older backup, when `OIDC_ISSUER` points at the wrong Keycloak (another realm, another instance), or when a non-importing user-storage provider (LDAP) is disabled for maintenance; a mistaken bulk deletion by an administrator looks the same. The gauge `inkcast_identity_sync_breaker_open` is read from the database, so the alert survives restarts and clears the moment the pass is resolved. Sign-in is unaffected. A person who deletes their own account is still erased meanwhile (the event is specific), and a person Keycloak reports disabled is still cut off (Keycloak has them; it is reversible).

1. The IdentityService log has one `identity.sync.breaker.tripped` line with the pass id (`pass_id`), how many were found gone (`gone`) of how many read (`read`), and then an `identity.sync.breaker.held` line at every reconcile (every `INKCAST_KEYCLOAK_RECONCILE_INTERVAL`, 10 minutes by default) with how many people are held now. `inkcast identity trips`, run in the pod that serves IdentityService (`oc exec deploy/inkcast -- inkcast identity trips`; `deploy/inkcast-identity-service` with the split-services component), lists every pass with its status. No person's id is in any of these lines.
2. Look at Keycloak before anything else: open a few of the people in its admin console (Users; the `identity` column of `users` holds `oidc:<issuer>#<id>`). Were they restored from a backup that predates them? Does `OIDC_ISSUER` name the realm and instance the people signed in through? Is a user federation provider disabled (User federation; a disabled LDAP provider makes every one of its users a 404 for the admin API)? Keycloak's own log and `admin-events` say what changed.
3. **Keycloak was wrong, and is fixed** (the people are back): `inkcast identity dismiss-deletions --pass <id> --by <you>`. Nothing is done to anyone; the next reconcile reads everyone again, finds them present, and the alert clears. If it was not fixed after all, the reconcile trips again with a new pass id.
4. **The people really are gone** (an administrator deleted them on purpose, a realm was purged): `inkcast identity confirm-deletions --pass <id> --by <you>`. Exactly the people of that pass are cut off (tokens, codes, PATs) and marked gone from now, so the reconcile erases them `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` later (at its next pass with `0`; never with `off`), after reading Keycloak again. One who signed in since the pass read them is spared (Keycloak let them in), and one erased meanwhile is in no pass any more. Anyone gone since the trip but not in the pass is read afresh at the next reconcile, and the breaker judges them anew. Confirming a pass Keycloak was wrong about cuts everyone in it off: they stay cut off until they sign in again (the next reconcile stops their grace period once Keycloak has them), and their PATs are gone for good. When in doubt, dismiss.
5. Both are claimed once and audited: the pass row (`sync_breaker_trips`: status, time, who) and the log line (`identity.sync.breaker.confirmed` or `identity.sync.breaker.dismissed`: pass id, who, counts). A second confirmation, or a dismissal after a confirmation, does nothing and logs `identity.sync.breaker.refused`.
6. `INKCAST_KEYCLOAK_BREAKER=off` turns the breaker off (a posture warning in production and staging): nothing is held, a pending pass included, and nothing trips. Raise `INKCAST_KEYCLOAK_BREAKER_MIN` or `_PERCENT` instead if a realm legitimately loses many people at once.

## InkcastAuthzErrors

**Meaning:** permission checks (E3.F3.6) have been failing for 5 minutes. Inkcast fails closed: every check the engine could not answer became a 503 for the person asking, and nothing was granted or changed. Readiness also reports the engine (`authz (openfga): …` in `/readyz`), so with several replicas the ones that can't reach OpenFGA stop receiving traffic.

1. The log has `authz.error` lines with the operation and the reason (ids only, never a token).
2. "OpenFGA unreachable" or "request failed (ReadTimeout)": OpenFGA is down, or a NetworkPolicy blocks it (E3.F3.9 lets only Inkcast services reach it). Check its pods and `INKCAST_FGA_API_URL`; `INKCAST_FGA_TIMEOUT` (5 s) is per request.
3. "refused the credentials (HTTP 401)": the preshared key in OpenFGA and `INKCAST_FGA_API_TOKEN(_FILE)` differ (a rotation done on one side only). Set both to the same value; the file is re-read without a restart.
4. "store or model not found (HTTP 404)" or `authorization_model_not_found`: `INKCAST_FGA_STORE_ID` or `INKCAST_FGA_MODEL_ID` names something that store does not hold. Each deploy pins a model id; after a new model version is written, its id must reach the ConfigMap (docs/authorization.md §8).
5. "too deep": a relationship chain longer than OpenFGA's resolution depth (25). Look for a cycle in the tuples written for that object.
6. With `INKCAST_AUTHZ_ENGINE=local` the engine is the Inkcast database itself: the storage probe will be failing too.

## SLO burn-rate alerts

Each SLO has four alerts named `InkcastSloBurn<Name>`; the labels say which window pair fired. `critical` pairs (14.4x over 1 h, 6x over 6 h) page: at that rate the 30-day budget is gone in two days or five. `warning` pairs (3x over 1 d, 1x over 3 d) open a ticket. Dashboards show `inkcast_slo:<name>:error_ratio_1h` against `1 - objective`.

### slo-api-availability

Non-5xx answers on `/api/*` and `/mcp`, objective 99.9%. Burning: follow `InkcastHttp5xxRateHigh`.

### slo-api-latency

Requests under one second, objective 99%. Burning: `inkcast_http_request_duration_seconds` by route; slow routes are usually the ones that call a platform (`destinations`, `publish now`) or a slow disk under SQLite.

### slo-publish-success

Attempts a platform accepted over all attempts, objective 99%. Burning: `InkcastPublishFailureRateHigh` by platform; unconfirmed sends count against it.

### slo-schedule-timeliness

Scheduled posts claimed within two minutes of their time, objective 99%. Burning: `InkcastPostsOverdue` and `InkcastQueueHeadLagHigh`.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
