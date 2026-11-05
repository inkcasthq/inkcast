# Runbooks: what to do when an Inkcast alert fires

Every alert in `src/inkcast/libs/alerting.py` links here by its name (a test checks the anchors). Each section says what the alert means, how to confirm it, what to do first, and when to escalate. The generated rules are in `deploy/kustomize/components/prometheus-rules/`; regenerate with `python scripts/gen-alerts.py`.

**Namespace.** The commands below write the app's namespace as `$NS`. Set it once before you start: `NS=inkcast` for a self-host from `deploy/kustomize/overlays/openshift` or `overlays/kubernetes`, `NS=inkcast-staging` for `deploy/kustomize/environments/staging`, and `NS=inkcast-prod` for `deploy/kustomize/environments/production`. Commands without `-n` use your current project, so run `oc project "$NS"` first (with `kubectl`: `kubectl config set-context --current --namespace "$NS"`).

**First five minutes, for any page:** `oc get pods -n "$NS"` (or `kubectl`), `oc logs deploy/inkcast-<service> --since=15m | grep '"level":"error"\|"level":"critical"'`, then `curl -s :9090/readyz` on the pod. Log events are catalogued in `libs/observability.EVENTS`; the `event` field is what to grep.

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
3. Exit code 78 in the previous container log means the process refused to start, and the `config.unsafe` log line says why: a configuration problem, or, with OpenFGA pinned by store name (`INKCAST_FGA_STORE_NAME`), OpenFGA not answering, refusing the key, holding two stores of the name or no version equal to the release's model. A pod stuck in `Init:` (`Init:CrashLoopBackOff`) is its init container `authz-model` failing to write the store and the model: `oc logs <pod> -c authz-model` says why (OpenFGA not serving yet on a fresh install, a wrong key).

## InkcastSchedulerStalled

**Meaning:** no scheduler tick in 10 minutes. Scheduled posts stay in the queue.

1. `oc get pod -l inkcast.io/service=scheduler-worker` (in `All` mode, the single pod). Readiness reports `scheduler: no tick recently`.
2. Look for `scheduler.tick.failed` in its log: a crashing tick retries every interval, and the traceback says why (usually storage).
3. Restart the pod if it is wedged; a tick in progress finishes first (SIGTERM is honoured), so nothing is half-sent.
4. After recovery, posts interrupted mid-send are resolved by `publish.recovered`; check for `unconfirmed` ones (next runbook).

## InkcastPostsOverdue

**Meaning:** posts made before their time have waited more than `INKCAST_OVERDUE_GRACE` (600 s) past it. The scheduler is stopped, behind, or every tick fails. A post made after its time (a Git file dated in the past, due at once: [ADR-0015](../adr/0015-publish-at-is-always-scheduled.md)) is a backfill and is never overdue ([ADR-0019](../adr/0019-a-backfill-post-has-no-deadline.md)): an import draining at the batch rate shows as head-of-line lag, below.

1. Is `InkcastSchedulerStalled` also firing? Follow it first.
2. `inkcast_posts_oldest_due_age_seconds` and `inkcast_posts_due` on the dashboard: a growing queue with ticks running means the batch (20 per tick) is too small for the burst, or a platform is slow (`inkcast_publish_duration_seconds`). Scale the scheduler (E3: several replicas are safe; the claim is atomic) or lower `INKCAST_SCHEDULER_INTERVAL`.
3. Posts that go out late are still published once; customers see the lag in their on-time rate.

## InkcastQueueHeadLagHigh

**Meaning:** the oldest due post has waited over 5 minutes but is not yet overdue. Early warning for `InkcastPostsOverdue`; same checks, no page. A Git import of files dated in the past drains here at 20 a tick (900 files: 15 minutes) and never becomes overdue ([ADR-0019](../adr/0019-a-backfill-post-has-no-deadline.md)): `inkcast_posts_due` falling at the batch rate is that, not a fault.

## InkcastUnconfirmedPublishes

**Meaning:** a publish ended `unconfirmed`: the request was sent and the platform's answer was lost (timeout or 5xx after the send) or named neither the post's URL nor its id (a 204, an empty 202: `accepted the request but its answer named neither` in the post's `error`), or the platform plug-in crashed mid-send (`internal error` in the post's `error`: a plug-in bug, roll back the image), or a post interrupted mid-send stayed `recovering` for `INKCAST_PUBLISH_RECOVERY_MAX_HOURS` with the platform never saying whether it arrived (`publish.recovery.capped` in the log; see `InkcastPostRecoveringTooLong`), or, on a platform Inkcast can't ask (Medium), a post whose send never began because its credentials could not be decrypted (`publish.recovery.unsent` in the log; see `InkcastPostsCannotBeSent`, step 4): that one is not on the platform. Inkcast never retries these blindly.

1. Find them: the admin portal (E6) or `SELECT id, user_id, platform, title FROM posts WHERE status='unconfirmed'`.
2. The person decides: after checking the platform, `inkcast_resend` (or `POST /api/v1/posts/{id}/resend`) sends the post again as exactly one new post and cancels the old one, or `inkcast_cancel` (or `POST /api/v1/posts/{id}/cancel`) cancels it. For a post whose credentials could not be decrypted before its send, the old post's error says it was not sent. For other posts, either reply says the post may already be on the platform. Inkcast never sends an unconfirmed post again on its own, and Git sync never creates one again: a Git-synced file whose post is unconfirmed is reported as needing that decision on every push (the sync still asks `find_existing`, and records a post the platform does have). Only a post the person will not act on is resolved by hand: if it exists on the platform, record it (`result_url`, `remote_id`, status `published`); if not and it must go out, set `scheduled` with `run_at=now` and `recovering_since=NULL` (the cap's clock), and say so in the ticket.
3. A burst of unconfirmed posts on one platform means that platform is timing out: see `InkcastPublishFailureRateHigh`.

## InkcastPostRecoveringTooLong

**Meaning:** a post whose send was interrupted (the process stopped mid-send: a deploy, an OOM kill, a node drain) has been `recovering` for over an hour. Every stuck pass (10 minutes) the scheduler asks the platform whether the post arrived (`find_existing`), and the platform keeps refusing the lookup for a reason that should pass: rate-limited (a 429), unreachable, or a 5xx. Recovery only asks, it never sends; so the post is neither out nor shown to anyone as unconfirmed. The age is `inkcast_posts_recovering_oldest_age_seconds`, read from the database (`posts.recovering_since`), so it survives restarts.

1. Find them: `SELECT id, user_id, platform, title, error, recovering_since FROM posts WHERE status='recovering'`. The `error` column holds the platform's last refusal; the `publish.recovery.deferred` log lines have the same, one per pass. A post whose recovery could not even be made (its credentials sealed under a previous `INKCAST_SECRET_KEY`, a payload this version can't read) is named by a `publish.recovery.failed` line instead, with the traceback, one per pass, and counted in `inkcast_publish_total{status="recovery_failed"}`; it holds up no other post, and is taken again at the next pass until the cause is gone (the person reconnects) or the cap ends it. `InkcastPostsCannotBeSent` fires for it: follow that runbook first.
2. A whole platform refusing (every recovering post on it, or `InkcastPublishFailureRateHigh` too): the platform is rate-limiting or down; nothing to do but wait, or check its status page. One account only: its credentials may have been revoked in a way the plug-in reads as transient; have the person reconnect (`inkcast_connect`).
3. The person can decide now rather than wait: `inkcast_cancel` (or `POST /api/v1/posts/{id}/cancel`) cancels a `recovering` post, and a cancel wins over an answer the platform gives afterwards. A cancelled post is never sent; if it did arrive, it stays on the platform, and the reply says so.
4. Left alone, recovery ends after `INKCAST_PUBLISH_RECOVERY_MAX_HOURS` (24 by default, 1 to 168, counted from the first interruption even if a re-send is interrupted again): the platform is asked one last time, and a post it has is recorded (`publish.recovered`); otherwise, "missing" included, the post is marked `unconfirmed` (`publish.recovery.capped`), `InkcastUnconfirmedPublishes` fires, and that runbook applies: the person cancels or resends it (`inkcast_resend`, or `inkcast resend <post-id>` from the CLI), and Git sync never creates it again. A post is never sent late without a person deciding. Lower the setting on a platform whose lookups stay blocked for days; it is a policy value, stated in the staging and production overlays.

## InkcastPostsCannotBeSent

**Meaning:** the scheduler took a post and could not even begin its send (`publish.send.failed`) or its recovery (`publish.recovery.failed`): its credentials can't be decrypted, or its payload can't be read (a newer version wrote it, and this one was rolled back). Both lines name the post and the platform, with the traceback, and both are counted in `inkcast_publish_total` (`status="send_failed"`, `status="recovery_failed"`). The scheduler goes on ticking and sends everyone else's posts, so `InkcastSchedulerStalled` does not fire. A send failure means this attempt sent nothing. A recovery failure cannot establish whether an earlier send arrived. Each stays `recovering` until `INKCAST_PUBLISH_RECOVERY_MAX_HOURS`, except on a platform Inkcast can't ask (Medium), where one whose credentials can't be decrypted is `unconfirmed` at the next stuck pass, for its owner to reconnect and resend (step 4). Credentials that can't be decrypted were sealed under another `INKCAST_SECRET_KEY` than the scheduler runs on: every person's posts fail when the scheduler's key is wrong, one person's when only theirs were sealed under another key.

1. Read one line's traceback. `Stored credentials can't be decrypted` (`CredentialsUnreadable`) is the key: step 2. A `TypeError` out of `Prepared.from_dict` is a payload this version can't read, after a rollback: roll forward to the version that wrote it.
2. **Check the key.** Compare the key each pod mounts with the Secret's; only digests are printed, never the key:
   - the Secret: `oc -n "$NS" get secret inkcast-secrets -o jsonpath='{.data.secret-key}' | base64 -d | sha256sum`
   - the scheduler: `oc -n "$NS" exec deploy/inkcast-scheduler-worker -- sha256sum /var/run/secrets/inkcast/secret-key`, and the same for `deploy/inkcast-connector-service` (it seals what a person connects) and `deploy/inkcast-publishing-api` (it reads them for a resend); with every service in one pod, `deploy/inkcast`.

   A pod whose digest differs mounts another Secret: fix its Deployment in the overlay. A process reads the key once, at its start, so a pod started before the Secret last changed runs on the old key whatever its file says now: `oc -n "$NS" rollout restart deploy/<name>`, as [the env-secrets runbook](env-secrets.md) says for a new `secret-key`.
3. Every pod on the Secret's key and the lines go on: those credentials were sealed under a key no pod has now (the key was replaced, or the database came from another environment), and are unreadable for good. The person connects the platform again (`inkcast_connect`, or `POST /api/v1/platforms/{platform}/connect-link`). A resend before that answers 409 `not_connected` naming that step, and cancels nothing (`publish.resend.unreadable` in the API's log).
4. What the posts become meanwhile. On a platform Inkcast can ask whether a post arrived (DEV), the post stays `recovering`, asked about at every stuck pass (10 minutes), and is sent once its credentials can be read (`InkcastPostRecoveringTooLong` after an hour; unconfirmed at `INKCAST_PUBLISH_RECOVERY_MAX_HOURS`). On one it can't (Medium), the next stuck pass marks a post whose credentials could not be decrypted `unconfirmed` (`publish.recovery.unsent` in the scheduler's log), and its owner sees: "Stored credentials could not be decrypted when this post was due (they were sealed under another INKCAST_SECRET_KEY than the scheduler runs on), so it was not sent. Reconnect Medium (inkcast_connect, or POST /api/v1/platforms/medium/connect-link), then resend it (inkcast_resend, or POST /api/v1/posts/{id}/resend) or cancel it (inkcast_cancel, or POST /api/v1/posts/{id}/cancel)." It is counted `unconfirmed`, so `InkcastUnconfirmedPublishes` fires for an hour, while this alert resolves 30 minutes after the last post that could not be sent. Nothing went out, so the resend sends it once. If the corrected key decrypts the stored credentials, the owner can resend without reconnecting. Otherwise, the owner reconnects. A post whose payload can't be read stays `recovering` on either kind of platform, counted `recovery_failed` at every stuck pass, so this alert stays on while it waits.

## InkcastPublishFailureRateHigh

**Meaning:** more than 20% of attempts to one platform failed in 30 minutes (with at least 5 attempts).

1. The error text is in the posts' `error` column and in the `publish.*` log lines; 401/403 means revoked credentials (customers must reconnect; E7 channel health will tell them), 5xx or timeouts mean a platform outage (a plug-in crash ends `unconfirmed`: see `InkcastUnconfirmedPublishes`).
2. Check the platform's status page; nothing to do for an outage except watch retries (`INKCAST_PUBLISH_MAX_ATTEMPTS` sends, `INKCAST_PUBLISH_RETRY_DELAYS` apart: by default 4 sends, 60 s, 5 m and 30 m apart; a 429's `Retry-After` can stretch a wait up to the largest delay) and unconfirmed posts.

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
4. A consumer name the code no longer has (`CONSUMERS` in `libs/events.py`) is a retired one, or one a pod of another release still polls during a rollout or a rollback: the scheduler removes its cursor once no pod has polled under it for an hour (`RETIRE_IDLE`), at its next start or housekeeping, so within 90 minutes (`outbox.consumer.retired` in its log), and the series goes with it.

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
2. A failing *startup* probe right after a rollout, with `identity.erasure_hash.backfill` lines in the previous container's log (`oc logs --previous`), is the one-off fill of `users.erasure_hash` outlasting the probe's 60 seconds on a large database: each start gets further, and nothing is lost or filled twice. See [Upgrading to the hash column](backup-restore.md#upgrading-to-the-hash-column).

## InkcastDataVolumeNearlyFull

**Meaning:** the SQLite volume has under 10% free.

1. Grow the PVC (most storage classes allow online expansion) or purge: housekeeping runs every 30 minutes (timed by the `housekeeping` heartbeat in the database, so restarts don't put it off; `scheduler.housekeeping.failed` in the scheduler's log when it crashes) and drops expired tokens, tickets, outbox rows older than `INKCAST_OUTBOX_RETENTION_DAYS` (7) that every consumer has passed, and hourly KPI rows older than `INKCAST_KPI_HOURLY_RETENTION_DAYS` (90); shortening either frees space at the next run. A consumer behind holds every outbox row it hasn't passed, whatever the setting: see InkcastOutboxConsumerLag first.
2. With PostgreSQL (E3) this alert belongs to the database operator.

## InkcastBackupMissing

**Meaning:** the `inkcast-backup` CronJob (the `backup` component) has not succeeded for more than 26 hours, so a lost volume would lose more than a day.

1. `oc get cronjob inkcast-backup` and `oc get jobs -l app.kubernetes.io/component=backup`; the failed Job's log has a `backup.failed` line with the reason, or a `backup.done` line followed by `backup.retention.refused` (a retry of the Job has that line alone: it found the day's copy and took no other, #471): the backup was taken and is good, but the newest earlier backup is `INKCAST_BACKUP_KEEP` days old or more (2 or more with `1`), so nothing aged out (#405). That is the node's clock jumped ahead (check `date -u` on the node; fix it, nothing else is needed) or a gap that long in backups (nothing is needed: the next good backup removes them). Details: [backup and restore runbook](backup-restore.md#how-a-backup-works).
2. Common reasons: the backup volume is full (lower `--keep` or grow `inkcast-backups`), or the Job could not schedule next to the Inkcast pod (both mount the ReadWriteOnce data volume, so they must share a node).
3. Take one now: `oc create job inkcast-backup-now --from=cronjob/inkcast-backup`. Restores: [backup and restore runbook](backup-restore.md).
4. Needs kube-state-metrics (OpenShift ships it). With PostgreSQL (E3) this alert moves to the database operator's backups.

## InkcastIdentitySyncFailing

**Meaning:** every pass of the Keycloak user sync (E9.F9.5.S2) failed for 15 minutes. People disabled or deleted in Keycloak can't sign in again, but the OAuth tokens and personal access tokens Inkcast already gave them keep working until the sync recovers or the tokens expire. Sign-in is not affected: readiness deliberately ignores the sync.

1. The IdentityService log has `identity.sync.failed` lines with the reason.
2. "Keycloak refused the sync service account (HTTP 401)": the `inkcast-sync` client secret in Keycloak and `INKCAST_KEYCLOAK_SYNC_SECRET` differ (a rotation done on one side only). Set both to the same value. "Keycloak's token endpoint answered HTTP 5xx: Keycloak is failing, not refusing the sync service account" is not that (#182): Keycloak, or a gateway in front of it (502, 503, 504), is failing for everyone; the secret is fine, and step 4 applies.
3. "HTTP 403" on `admin-events` or `events`, or "the reconcile could act on nobody" with an `identity.sync.person.failed` line (`status` 403) for every person: the service account lost `view-users` or `view-events` (realm-management). The realm file grants both.
4. Connection errors and timeouts ("no answer from Keycloak's token endpoint", "no answer from Keycloak to the user read (ConnectError): on the way there, not about a person", or the error itself on `admin-events` or `events`): Keycloak is down, or a NetworkPolicy blocks the admin API (same host and port as the issuer). A connection refused or reset, a connect timeout or the client's own pool exhausted (`ConnectError`, `ConnectTimeout`, `PoolTimeout`) happens before a request reaches Keycloak, so it is never one person's: it fails the pass at once (#152). A read about one person that reached Keycloak and got no answer (`ReadTimeout`, `ReadError`, `RemoteProtocolError`, or a 504 from a gateway in front of Keycloak that gave up waiting, `GatewayTimeout`) is that person's alone (`InkcastIdentitySyncPersonFailing`, #121), once a connection that failed under the read at once (`ReadError`, `RemoteProtocolError` within a second: a keep-alive connection a router closed while idle, a Keycloak pod restart) has been read again on another connection (#182), up to a budget of three such reads per pass: at the third the pass ends, and it fails ("no answer from Keycloak to 3 reads about people, and no read about a person answered since the pass began") only when no read about a person answered since it began. That is a user-storage provider down for everyone (Keycloak's own lists answer; every person hangs; the pass costs three timeouts, not one per person), or a pass that read only people who hang (their `identity.sync.person.failed` lines name them; see that alert). After a reconcile that could read nobody, everyone is read again at the next scheduled reconcile, not at every pass (#119); the passes until then fail with "the last reconcile could act on nobody".
5. Once it recovers, the next full reconcile (every `INKCAST_KEYCLOAK_RECONCILE_INTERVAL`, 10 minutes by default, and at start) catches anyone missed. To force it now, restart IdentityService.
6. A self-deletion whose `DELETE_ACCOUNT` event Keycloak trimmed meanwhile (the realm keeps events 7 days) is still cut off by that reconcile, and erased `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` (30) later, like an administrator's deletion. An outage longer than the retention therefore delays those erasures by the grace period; it never loses them.

## InkcastIdentitySyncPersonFailing

**Meaning:** for an hour, every 30 minutes has had a Keycloak user sync failure at one stage (`stage="person"`: Keycloak answered an error about one person, a 500 for that user or a malformed id, on `users/{id}` or their `DELETE_ACCOUNT` events, or gave that read no answer once it reached Keycloak: a timeout, or the connection closed without an answer, #121). The pass went on past them (#103), so everyone else is cut off and erased as usual. For that person: if their `users/{id}` read fails (`call=user`), nothing is done until Keycloak answers about them, so their tokens keep working if Keycloak has disabled or deleted them meanwhile; if Keycloak answered that they are gone but the read of their `DELETE_ACCOUNT` events fails (`call=events`), they are cut off at once and only the erasure decision waits. The sync as a whole is fine; if `InkcastIdentitySyncFailing` fires too, follow it first (a realm-wide 403 or 500 fails everyone, which also fails the reconcile as a pass; a connection refused or reset is nobody's and fails the pass at once; a pass whose budget of three reads with no answer is used before any read about a person answered fails too: an outage, or only people who hang, #152).

1. The IdentityService log has `identity.sync.person.failed` lines: `user_id` (the Inkcast id; every pass reads only people with an Inkcast account, so someone another application's administrator edits in a shared realm is never read, named or counted, #181), `via` (`event` or `reconcile`), `call` (`user` for `users/{id}`, `events` for their `DELETE_ACCOUNT` events), the HTTP `status` (`0` when there was no answer) and `cause` (the transport error's name when there was none: `ReadTimeout`, `ReadError` or `RemoteProtocolError`, or `GatewayTimeout` for a gateway's 504; empty when Keycloak answered). A `ReadError` or `RemoteProtocolError` that came back at once (within a second: a keep-alive connection closed while idle, a pod restart) is the connection's, not the person's: the read was made again on another connection before anything was counted (#182), so a line with that `cause` means the second read failed too, or the connection was cut after waiting (a proxy or firewall giving up on a lookup that hangs), which is the hang itself. Nothing from Keycloak's answer or from the error is logged. The Keycloak id is the `identity` column of that user (`oidc:<issuer>#<id>`).
2. The person is read again at every reconcile (every `INKCAST_KEYCLOAK_RECONCILE_INTERVAL`, 10 minutes by default), and at every pass (30 s) while a `DELETE_ACCOUNT` event of theirs holds the cursor or an edit of theirs is within the minute before the admin-events cursor, but once per pass: a pass that got no answer about them does not read them again. The alert clears about 30 minutes after the last failure (it looks at the last 30 minutes, with no hold).
3. The fault is in Keycloak, about that one user: open them in its admin console (Users, search by the id), which makes the same `users/{id}` read, and Keycloak's own log says why it fails (`status` 500), refuses the id (400) or never answers (`status` 0, `cause` `ReadTimeout`: a user-storage provider's lookup that hangs, such as an LDAP referral to an unreachable server; the sync's client gives up after 30 s). Fix or delete the user there: a deletion answers 404 at the next read, and the sync cuts them off and erases them after the grace period.
4. A `DELETE_ACCOUNT` event of that person holds the user-events cursor (`sync_state`, `keycloak-user-events`), so every pass re-reads the events since it; that is by design (an erasure is never passed over) and costs one read per pass until they are erased. If the person is gone and that event is all that is left, nothing needs doing: the first read that answers 404 erases them.
5. A read with no answer costs the pass its timeout (30 s) every time it is made, and a pass has a budget of three such reads (#152): at the third it ends early, as a success for what it did, with one `identity.sync.pass.ended` line (the stage it ended in and the count). The `DELETE_ACCOUNT` cursor stops before every event not acted on, admin events not reached wait for the reconcile (which reads everyone), and a reconcile acts on whoever it read, but for the people the breaker would hold while the people it left untried, not known to hang, could take the breaker's count to `INKCAST_KEYCLOAK_BREAKER_MIN`: those wait for a reconcile that reads everyone it can. When it left such people untried, the next reconcile is at the next pass, which reads the people found hanging last; otherwise the next is on schedule. So one or two people whose reads hang cost each pass their timeouts and block nobody, wherever they are in the reconcile's order: it reads the people whose last read got no answer last, the longest ago first, so they delay each other only, and in the pass that reconciles the event passes leave those people to it, so that everyone else is read first even when three or more hang. A pass that read only people who hang, though (three with a pending `DELETE_ACCOUNT` event each, or edited within the same minute, in a pass between reconciles), has no answer about anyone and fails as a whole, the cursors unmoved; `InkcastIdentitySyncFailing` does not fire while the reconciles succeed, but `InkcastIdentitySyncReconcileEndingEarly` does once every reconcile ends on the budget (#180). Each such person is named in `identity.sync.person.failed` at every pass that reads them; fix them in Keycloak (step 3). Nothing is done to a person whose read got no answer: they are neither gone nor present, so the breaker (`InkcastIdentitySyncBreakerOpen`) neither counts them nor holds them. The `/account/return` page reads the person coming back from Keycloak through the same client; that read answers or not on its own and counts for no pass.

## InkcastIdentitySyncReconcileEndingEarly

**Meaning:** for an hour, every 30 minutes has had a reconcile of the Keycloak user sync end early on its timeout budget: three reads about people got no answer, so it stopped reading (#152, #180). A pass that ends this way is a success for what it did (`inkcast_identity_sync_total{result="ok"}`), so `InkcastIdentitySyncFailing` cannot see it; `inkcast_identity_sync_ended_early_total{stage="reconcile"}` counts it. It means at least three people hang at every reconcile: a user-storage provider down for its own people while local users answer (a partial outage), or that many users whose lookup hangs (LDAP referrals to an unreachable server). The reconcile reads the people known to hang last, so everyone who answers is read and acted on first, and with exactly three hanging people nothing else is lost; with more, they take turns (three per reconcile, the longest untried first), and one who recovers and is then disabled with no admin event waits up to `ceil(H/3)` intervals. Sign-in is unaffected.

1. `identity.sync.pass.ended` lines (`stage`, `timeouts`) say which stage ended each pass; `identity.sync.person.failed` lines with `status` `0` name the people whose reads got no answer (`InkcastIdentitySyncPersonFailing`, which fires too). The Keycloak id is the `identity` column of each user. Many people from one user federation provider means that provider; a few means those users.
2. Is `InkcastIdentitySyncStale` firing too? Then the reconciles are also leaving people who would answer untried (new people hang at every reconcile): follow that alert first. If not, everyone who can answer is being read on schedule, and only the people who hang are stale.
3. The fault is in Keycloak: check the provider (User federation; its own log and Keycloak's), or open one of the people in the admin console (the same `users/{id}` read). Fix or delete the user there (`InkcastIdentitySyncPersonFailing`, step 3); for a provider that is down, nothing in Inkcast needs doing but waiting: once it answers, the next reconcile reads everyone and the alert clears within an hour.
4. Nothing is done to a person whose read got no answer: they are neither gone nor present, so the breaker (`InkcastIdentitySyncBreakerOpen`) neither counts nor holds them, and their tokens keep working until Keycloak answers about them.

## InkcastIdentitySyncStale

**Meaning:** the Keycloak user sync has not reconciled everyone it could for more than three times `INKCAST_KEYCLOAK_RECONCILE_INTERVAL` (#180). `inkcast_identity_sync_reconcile_age_seconds` is the time since the last reconcile that read every person not known to hang (`sync_state`, `keycloak-reconciled`, written by the sync and read by every process, so it survives restarts); `inkcast_identity_sync_reconcile_interval_seconds` is the setting. Until the first such reconcile after a database is new, the age runs from the process start. People disabled or deleted in Keycloak with no admin event (an LDAP-imported user disabled in the directory) keep their Inkcast tokens until a full reconcile reads them. Sign-in is unaffected.

1. Is `InkcastIdentitySyncFailing` firing? Then every pass fails and no reconcile completes: follow it first. Is the IdentityService pod restarting (`InkcastPodRestarting`)? A crash loop never finishes a reconcile; the age keeps counting across restarts, which is why it is in the database.
2. Neither, and every pass succeeds: the reconciles end on their budget (`InkcastIdentitySyncReconcileEndingEarly`, `identity.sync.pass.ended` with `stage` `reconcile`) with people untried who are not known to hang, so none counts as full and the next is due at the next pass. That is reads that time out at random (a flaky provider or network: each reconcile finds three new people who hang, reads them last next time, and finds three more), and the people beyond them in the order are never reached. The `identity.sync.person.failed` lines name a different three each pass. Fix the provider or the path to Keycloak; once reads answer, one reconcile reads everyone and the alert clears.
3. Nothing ends early and nothing fails, yet the age grows: a pass that never finishes. `inkcast_identity_sync_total` stops moving (it counts passes that end, either way) and the log has no `identity.sync.*` line since. Every read answers, slowly (under the client's 30 s timeout, so nothing is counted) and a realm of many people takes longer than the interval, or the pass is wedged on something that never answers and never times out. Keycloak's own request log says which; restarting IdentityService starts afresh with a reconcile at once.
4. The threshold follows the setting: three intervals is one reconcile that ended on its budget with people untried (due again at the next pass), plus the next on schedule, plus slack; a legitimately slow realm needs a longer `INKCAST_KEYCLOAK_RECONCILE_INTERVAL` (under 1800: see the README), not a longer alert.

## InkcastIdentitySyncBreakerOpen

**Meaning:** a full reconcile of the Keycloak user sync found at least `INKCAST_KEYCLOAK_BREAKER_MIN` (5) people, and more than `INKCAST_KEYCLOAK_BREAKER_PERCENT` (10) of the people it read, newly gone from Keycloak (404 `User not found`, no `DELETE_ACCOUNT` event of theirs) or newly disabled in it (`enabled: false`), counted together, and the circuit breaker tripped (E9.F9.5.S7, issue #105; the disabled kind, E9.F9.5.S8, issue #135); or the admin-events passes of one `INKCAST_KEYCLOAK_RECONCILE_INTERVAL` found that many by USER admin events, of the people Inkcast knows from this Keycloak, the people the passes before cut off counted with this pass's (issue #139: a scripted mass deletion or disabling through the admin API, a bad script or a compromised administrator account, arrives one event at a time, and a person already cut off is not newly gone to the reconcile). The pass holds each person by kind, and keeps holding whoever Keycloak says the same about afterwards, until an operator decides:

- **Gone:** nobody was cut off or erased, and no erasure grace period started or advanced.
- **Disabled:** their OAuth access and refresh tokens, codes and pending consents were cut at once (a sign-in restores them, and Keycloak refuses that while they are disabled, so this is reversible); their personal access tokens are **held**, not revoked: a request with one answers `401` with `error: token_held` ("on hold while an operator reviews a change at the sign-in provider"), and `inkcast sync` fails as it would with a revoked token.

Keycloak answers 404 about people who still exist when it was restored from an older backup, when `OIDC_ISSUER` points at the wrong Keycloak (another realm, another instance), or when a non-importing user-storage provider (LDAP) is disabled for maintenance; a mistaken bulk deletion by an administrator looks the same. A disabled *importing* LDAP provider makes it report every one of its people disabled instead (Keycloak 26.6.1 `UserStorageManager.validateFederatedUser`); so does a bulk disable, and so does a realm restored from a backup taken while they were disabled. The gauge `inkcast_identity_sync_breaker_open` is read from the database, so the alert survives restarts and clears the moment the pass is resolved. Sign-in is unaffected (Keycloak decides it). A person who deletes their own account is still erased meanwhile (the event is specific).

1. The IdentityService log has one `identity.sync.breaker.tripped` line with the pass id (`pass_id`), which pass found them (`via`: `reconcile`, or `event` for the admin-events pass), how many were newly found gone (`gone`) and disabled (`disabled`) of how many read (`read`: for the admin-events pass, the people known, and `recent` says how many the passes before it cut off within the interval), one `identity.user.held` line per person held (Inkcast user id and counts), and then an `identity.sync.breaker.held` line at every reconcile (every `INKCAST_KEYCLOAK_RECONCILE_INTERVAL`, 10 minutes by default) with how many people are held now, by kind. `inkcast identity trips`, run in the pod that serves IdentityService (`oc exec deploy/inkcast -- inkcast identity trips`; `deploy/inkcast-identity-service` with the split-services component), lists every pass with its counts and status. No person's id is in the pass lines.
2. Look at Keycloak before anything else: open a few of the people in its admin console (Users; the `identity` column of `users` holds `oidc:<issuer>#<id>`). Were they restored from a backup that predates them? Does `OIDC_ISSUER` name the realm and instance the people signed in through? Is a user federation provider disabled (User federation; a disabled LDAP provider makes every one of its users a 404 for the admin API, or `enabled: false` when it imports them)? Keycloak's own log and `admin-events` say what changed.
3. **Keycloak was wrong, and is fixed** (the people are back, or enabled again): `inkcast identity dismiss-deletions --pass <id> --by <you>`. Nothing is done to the gone; the disabled are released and their personal access tokens work again (`identity.user.released` per person). The next reconcile reads everyone again, finds them present and enabled, and the alert clears. If it was not fixed after all, the reconcile trips again with a new pass id, and holds the disabled again. A held person Keycloak reports enabled again before you act is released by the pass that reads them, and leaves the pass, so you need not hurry to dismiss on their account; one who signs in again is released by the sign-in.
4. **Keycloak is right** (an administrator deleted or disabled them on purpose, a realm was purged): `inkcast identity confirm-deletions --pass <id> --by <you>`. Exactly the people of that pass are cut off, by kind: the gone (tokens, codes, PATs) are marked gone from now, so the reconcile erases them `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` later (at its next pass with `0`; never with `off`), after reading Keycloak again; the disabled have their personal access tokens revoked, and are never erased (Keycloak has them). One who signed in since the pass read them is spared (Keycloak let them in), one a pass has since read present (the gone) or enabled (the disabled) was released then, even as the confirmation runs (it claims each person before it acts, #161), and one erased meanwhile is in no pass any more. Anyone gone or disabled since the trip but not in the pass is read afresh at the next reconcile, and the breaker judges them anew (a person held meanwhile stays held until then). Confirming a pass Keycloak was wrong about cuts everyone in it off: they stay cut off until they sign in again (the next reconcile stops a gone person's grace period once Keycloak has them), and their PATs are gone for good. When in doubt, dismiss.
5. Both are claimed once and audited: the pass row (`sync_breaker_trips`: status, time, who) and the log line (`identity.sync.breaker.confirmed`: pass id, who, counts by kind, how many were marked gone but not cut off here, a pass having cut them off or released them between the mark and the cut-off, so their grace period runs all the same, how many disabled people another pass cut off meanwhile, and how many were skipped: signed in since, released, or erased; `identity.sync.breaker.dismissed`: pass id, who, how many released). A second confirmation, or a dismissal after a confirmation, does nothing and logs `identity.sync.breaker.refused`.
6. While a pass is held, the admin-events pass (every 30 s) holds too: a person an administrator deletes is left for the reconcile, as before, and one an administrator disables is held like the pass's people (OAuth cut, PAT refused), though not added to the pass; the first reconcile after the pass is resolved judges them afresh, and cuts them off as usual if they are alone. With the breaker closed both are cut off at once, as always, unless the pass's own count reaches the thresholds (issue #139): the pass reads everyone its events name before it acts, counts the people newly found gone or disabled together with those the admin-events passes of the last `INKCAST_KEYCLOAK_RECONCILE_INTERVAL` cut off, against the people known, and at the thresholds holds its people as a pass of its own, resolved by the steps above (`via: event` on the tripped line). The people the passes before it cut off are counted but not in the pass: they stay cut off (their tokens are gone, their PATs revoked), their erasure waits with the pass (the open breaker advances no grace period), and a sign-in restores their account once Keycloak has them again (after a dismissal, restore them in Keycloak; they mint new tokens at their next sign-in). A trip starts that count afresh, so one deletion after a dismissal is acted on alone.
7. `INKCAST_KEYCLOAK_BREAKER=off` turns the breaker off (a posture warning in production and staging): nothing is held, a pending pass included (its held people are cut off, their PATs revoked, at the next reconcile), and nothing trips. Raise `INKCAST_KEYCLOAK_BREAKER_MIN` or `_PERCENT` instead if a realm legitimately loses many people at once.

## InkcastAuthzErrors

**Meaning:** permission checks (E3.F3.6) have been failing for 5 minutes. Inkcast fails closed: every check the engine could not answer became a 503 for the person asking, and nothing was granted or changed. Readiness also reports the engine (`authz (openfga): …` in `/readyz`), so with several replicas the ones that can't reach OpenFGA stop receiving traffic.

1. The log has `authz.error` lines with the operation and the reason (ids only, never a token).
2. "OpenFGA unreachable" or "request failed (ReadTimeout)": OpenFGA is down, or a NetworkPolicy blocks it (E3.F3.9 lets only Inkcast services reach it). Check its pods and `INKCAST_FGA_API_URL`; `INKCAST_FGA_TIMEOUT` (5 s) is per request.
3. "refused the credentials (HTTP 401)": the preshared key in OpenFGA and `INKCAST_FGA_API_TOKEN(_FILE)` differ (a rotation done on one side only). Set both to the same value; the file is re-read without a restart.
4. "store or model not found (HTTP 404)" or `authorization_model_not_found`: the pinned store or model id names something OpenFGA does not hold (a store deleted or restored from a backup taken before the version was written). Pinned by name (`INKCAST_FGA_STORE_NAME`, the default), a restart re-resolves the store and the version, and the pod's init container `authz-model` writes them again first when they are gone. Pinned by hand (`INKCAST_FGA_STORE_ID` with `INKCAST_FGA_MODEL_ID`), the ids in the ConfigMap must be moved to what the store holds (docs/authorization.md §8).
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

Scheduled posts made before their time and claimed within two minutes of it, on the scheduler's clock, objective 99%. A post made after its time (a Git file dated in the past) has no time to be on time for and is not measured ([ADR-0019](../adr/0019-a-backfill-post-has-no-deadline.md)): an import of any size does not move the error ratio. Burning: `InkcastPostsOverdue` and `InkcastQueueHeadLagHigh`.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
