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

**Meaning:** Prometheus cannot scrape a pod's metrics port. The pod is down, not ready, or the NetworkPolicy no longer admits the monitoring namespace.

1. `oc get pods -l app.kubernetes.io/part-of=inkcast`; a pod not `Running`/`Ready` is the cause.
2. If pods look fine: `oc get networkpolicy inkcast -o yaml` and check the monitoring namespace selector; `oc get servicemonitor inkcast` must name port `metrics`.
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

**Meaning:** a publish ended `unconfirmed`: the request was sent and the platform's answer was lost (timeout or 5xx after the send). Inkcast never retries these blindly.

1. Find them: the admin portal (E6) or `SELECT id, user_id, platform, title FROM posts WHERE status='unconfirmed'`.
2. Check the platform for the post. If it exists, record it (`result_url`, `remote_id`, status `published`); if not, set `scheduled` with `run_at=now`. Git-published posts resolve themselves on the next sync (`find_existing`).
3. A burst of unconfirmed posts on one platform means that platform is timing out: see `InkcastPublishFailureRateHigh`.

## InkcastPublishFailureRateHigh

**Meaning:** more than 20% of attempts to one platform failed in 30 minutes (with at least 5 attempts).

1. The error text is in the posts' `error` column and in the `publish.*` log lines; 401/403 means revoked credentials (customers must reconnect; E7 channel health will tell them), 5xx or timeouts mean a platform outage, `internal error` means a plug-in bug.
2. Check the platform's status page; nothing to do for an outage except watch retries (60 s, 5 m, 30 m) and unconfirmed posts.
3. A plug-in bug: roll back the image; the posts failed for good and the customers must republish.

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

1. Grow the PVC (most storage classes allow online expansion) or purge: `purge_expired` runs every 90 ticks and drops expired tokens, tickets, outbox rows older than 7 days and hourly KPI rows older than 90 days.
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
3. "HTTP 403" on `users/…` or `admin-events`: the service account lost `view-users` or `view-events` (realm-management). The realm file grants both.
4. Connection errors: Keycloak is down, or a NetworkPolicy blocks the admin API (same host and port as the issuer).
5. Once it recovers, the next full reconcile (every `INKCAST_KEYCLOAK_RECONCILE_INTERVAL`, 10 minutes by default, and at start) catches anyone missed. To force it now, restart IdentityService.

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
