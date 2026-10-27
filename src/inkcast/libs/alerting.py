# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The alert catalogue and the SLOs as code. ``scripts/gen-alerts.py`` renders them into a
``PrometheusRule`` under ``deploy/kustomize/components/prometheus-rules`` (``--check`` in CI), and tests
keep every alert tied to a runbook section in ``docs/runbooks/alerts.md``.

Three kinds of rule come out:

* **symptom alerts** (``ALERTS``): something a person must act on, each with a severity, a routing label
  and a runbook. A failure a user would notice has an alert here (definition of done #6).
* **SLOs** (``SLOS``): an objective over a 30-day window on a ratio of good events to all events, with
  recording rules for the error ratio over seven windows;
* **burn-rate alerts** per SLO, the multiwindow, multi-burn-rate pattern from the SRE workbook: page when
  the budget burns 14.4x (1 h and 5 m) or 6x (6 h and 30 m), open a ticket at 3x (1 d and 2 h) or 1x
  (3 d and 6 h). The pairs keep an alert from paging on a blip and from staying red after a recovery.

Labels: ``severity`` (critical pages, warning tickets, info is for dashboards), ``team: inkcast`` and
``service``. Alertmanager routes on them (see docs/design/observability-and-kpis.md); nothing here names
a receiver, because receivers belong to the cluster, not the application.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RUNBOOK_BASE = "https://github.com/inkcasthq/inkcast/blob/main/docs/runbooks/alerts.md"
SEVERITIES = ("critical", "warning", "info")


@dataclass(frozen=True)
class Alert:
    name: str
    expr: str
    severity: str
    summary: str
    description: str
    service: str
    for_: str = "5m"
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def runbook(self) -> str:
        return f"{RUNBOOK_BASE}#{self.name.lower()}"

    def rule(self) -> dict:
        return {
            "alert": self.name, "expr": self.expr.strip(), "for": self.for_,
            "labels": {"severity": self.severity, "team": "inkcast", "service": self.service, **self.labels},
            "annotations": {"summary": self.summary, "description": self.description, "runbook_url": self.runbook},
        }


@dataclass(frozen=True)
class Slo:
    name: str                 # "api-availability"
    description: str
    objective: float          # 0.999
    good: str                 # a counter selector or a bucket selector; see ``error_ratio``
    total: str
    service: str
    window_days: int = 30

    @property
    def key(self) -> str:
        return self.name.replace("-", "_")

    @property
    def runbook(self) -> str:
        return f"{RUNBOOK_BASE}#slo-{self.name}"

    def error_ratio(self, window: str) -> str:
        return (f"1 - (sum(rate({self.good}[{window}])) / clamp_min(sum(rate({self.total}[{window}])), 1e-9))")


# Burn-rate windows: (long, short, burn rate, severity). SRE workbook, chapter 5, table 5-5 for a 30-day window.
BURN_WINDOWS: tuple[tuple[str, str, float, str], ...] = (
    ("1h", "5m", 14.4, "critical"),
    ("6h", "30m", 6.0, "critical"),
    ("1d", "2h", 3.0, "warning"),
    ("3d", "6h", 1.0, "warning"),
)
WINDOWS = ("5m", "30m", "1h", "2h", "6h", "1d", "3d")

_API_ROUTES = 'route=~"/api/.*|/mcp"'

ALERTS: tuple[Alert, ...] = (
    Alert("InkcastTargetDown", 'up{job=~".*inkcast.*"} == 0', "critical",
          "An Inkcast pod is not answering /metrics",
          "{{ $labels.instance }} ({{ $labels.pod }}) has not been scraped successfully for 5 minutes.",
          "platform"),
    Alert("InkcastSchedulerStalled",
          'sum(rate(inkcast_scheduler_ticks_total[10m])) == 0 or absent(inkcast_scheduler_ticks_total)',
          "critical", "The scheduler loop has stopped ticking",
          "No scheduler tick for 10 minutes: scheduled posts will not go out. Check the scheduler-worker pod and "
          "its readiness (it reports 'scheduler: no tick recently').", "scheduler-worker", for_="10m"),
    Alert("InkcastPostsOverdue", "max(inkcast_posts_overdue) > 0", "critical",
          "Scheduled posts are past due",
          "{{ $value }} post(s) are more than INKCAST_OVERDUE_GRACE past their time: the scheduler is behind, stuck "
          "or stopped.", "scheduler-worker"),
    Alert("InkcastQueueHeadLagHigh", "max(inkcast_posts_oldest_due_age_seconds) > 300", "warning",
          "The oldest due post has waited more than 5 minutes",
          "Head-of-line lag is {{ $value | humanizeDuration }}: ticks are slow (a platform is slow, or the batch is "
          "too small for the load). Not yet overdue.", "scheduler-worker", for_="10m"),
    Alert("InkcastUnconfirmedPublishes", 'sum(increase(inkcast_publish_total{status="unconfirmed"}[1h])) > 0', "warning",
          "Posts are unconfirmed and wait for a person",
          "{{ $value }} publish(es) in the last hour ended unconfirmed: the platform may or may not have the post. "
          "Someone must check the platform before anyone publishes again.", "publishing", for_="0m"),
    Alert("InkcastPublishFailureRateHigh",
          'sum by (platform) (rate(inkcast_publish_total{status=~"failed|update_failed"}[30m]))'
          ' / clamp_min(sum by (platform) (rate(inkcast_publish_total[30m])), 1e-9) > 0.2'
          ' and sum by (platform) (increase(inkcast_publish_total[30m])) >= 5',
          "warning", "More than 20% of publishes to {{ $labels.platform }} fail",
          "{{ $value | humanizePercentage }} of attempts to {{ $labels.platform }} failed over 30 minutes (at least 5 "
          "attempts). A platform outage, revoked credentials or a plug-in bug.", "publishing", for_="10m"),
    Alert("InkcastHttp5xxRateHigh",
          f'sum by (route) (rate(inkcast_http_request_duration_seconds_count{{code="5xx",{_API_ROUTES}}}[10m]))'
          f' / clamp_min(sum by (route) (rate(inkcast_http_request_duration_seconds_count{{{_API_ROUTES}}}[10m])), 1e-9) > 0.05',
          "warning", "5xx rate above 5% on {{ $labels.route }}",
          "{{ $value | humanizePercentage }} of requests to {{ $labels.route }} returned 5xx over 10 minutes.",
          "publishing-api", for_="10m"),
    Alert("InkcastGatewayUpstreamUnreachable", 'sum by (upstream) (increase(inkcast_gateway_requests_total{code="error"}[5m])) > 0',
          "critical", "The built-in Gateway cannot reach {{ $labels.upstream }}",
          "Requests to {{ $labels.upstream }} fail at the Gateway (502 upstream_unreachable). The service is down, "
          "its Service has no endpoints, or the NetworkPolicy blocks the Gateway.", "gateway"),
    Alert("InkcastOutboxConsumerLag", "max by (consumer) (inkcast_outbox_oldest_age_seconds) > 600", "warning",
          "Outbox consumer {{ $labels.consumer }} is 10 minutes behind",
          "Domain events wait {{ $value | humanizeDuration }} for consumer {{ $labels.consumer }}: KPIs are stale. A "
          "sink keeps failing (see outbox.consumer.failed in the logs) or the InsightsService pod is down.",
          "insights-service", for_="10m"),
    Alert("InkcastCacheBackendErrors", "sum(rate(inkcast_cache_errors_total[5m])) > 0", "warning",
          "Redis/Valkey is failing; rate limits are per replica",
          "Cache backend errors for 10 minutes. Services keep working on in-process fallbacks, so limits and locks "
          "are no longer shared across replicas.", "platform", for_="10m"),
    Alert("InkcastRateLimitSurge", 'sum(rate(inkcast_ratelimit_total{decision="limited"}[5m])) > 1', "info",
          "Unauthenticated writes are being rate-limited at more than 1/s",
          "Sustained 429s on registration, sign-in, tokens or connect pages: a client in a loop, or abuse. Consider "
          "an edge rule.", "identity-service", for_="15m"),
    Alert("InkcastPodRestarting", 'increase(kube_pod_container_status_restarts_total{container="inkcast"}[1h]) > 3',
          "warning", "{{ $labels.pod }} restarted more than 3 times in an hour",
          "A crash loop or a failing probe. Exit code 78 means a configuration problem (the config.unsafe log line "
          "lists it).", "platform"),
    Alert("InkcastDataVolumeNearlyFull",
          'kubelet_volume_stats_available_bytes{persistentvolumeclaim="inkcast-data"}'
          ' / kubelet_volume_stats_capacity_bytes{persistentvolumeclaim="inkcast-data"} < 0.1',
          "warning", "The SQLite volume has less than 10% free",
          "{{ $value | humanizePercentage }} of inkcast-data is free. Writes will start failing; grow the PVC or purge.",
          "platform", for_="15m"),
    Alert("InkcastBackupMissing",
          'time() - kube_cronjob_status_last_successful_time{cronjob="inkcast-backup"} > 26 * 3600',
          "critical", "No successful database backup for more than 26 hours",
          "The inkcast-backup CronJob has not succeeded since {{ $value | humanizeDuration }} ago. The SQLite file is "
          "the only copy of tokens, credentials, posts and the ledger until a backup succeeds.", "platform",
          for_="15m"),
    # Only where the sync is on (INKCAST_KEYCLOAK_SYNC_CLIENT_ID): elsewhere the series do not exist.
    Alert("InkcastIdentitySyncFailing",
          'sum(increase(inkcast_identity_sync_total{result="error"}[15m])) > 0'
          ' and sum(increase(inkcast_identity_sync_total{result="ok"}[15m])) == 0',
          "warning", "The Keycloak user sync has failed for 15 minutes",
          "Every Keycloak user sync pass for 15 minutes failed, so people disabled or deleted in Keycloak keep their "
          "Inkcast tokens until it recovers. Sign-in itself is unaffected.", "identity"),
    # #103: a failure Keycloak answers about one person is that person's alone; the pass goes on and reads them again
    # at the next reconcile (every 10 minutes by default), so a person it keeps failing for shows up in every 30 m.
    # #121: so is a read about one person that gets no answer (a timeout), unless the next read gets none either.
    Alert("InkcastIdentitySyncPersonFailing",
          'sum by (stage) (increase(inkcast_identity_sync_errors_total[30m])) > 0',
          "warning", "The Keycloak user sync keeps failing at stage {{ $labels.stage }}",
          "For an hour, every 30 minutes has had a Keycloak user sync failure at stage {{ $labels.stage }}: Keycloak "
          "answers an error about one person (users/{id} or their events), or gives that read no answer at all (a "
          "timeout), so they are neither cut off nor erased while everyone else is. The identity.sync.person.failed "
          "log lines name the Inkcast user id, the read, the HTTP status (0 for no answer) and the error's name. If "
          "InkcastIdentitySyncFailing fires too, follow it first.", "identity", for_="1h"),
    # #105: the gauge is read from the database (1 while a pass is pending), so it needs no hold and survives restarts.
    Alert("InkcastIdentitySyncBreakerOpen", "max(inkcast_identity_sync_breaker_open) > 0", "critical",
          "The Keycloak user sync holds a mass deletion or disabling for an operator",
          "A reconcile found at least INKCAST_KEYCLOAK_BREAKER_MIN people, and more than INKCAST_KEYCLOAK_BREAKER_PERCENT "
          "of those it read, gone from Keycloak (404 User not found) or disabled in it (enabled: false) at once. Nobody "
          "is cut off or erased and no grace period starts until an operator decides; the disabled have lost their OAuth "
          "tokens and their personal access tokens are refused, not revoked. A Keycloak restored from an older backup, the "
          "issuer pointing at the wrong Keycloak, or a user-storage provider switched off (its people answer 404, or "
          "disabled when it imports) all look like this. Check Keycloak, then `inkcast identity confirm-deletions --pass "
          "<id>` (Keycloak is right: the gone are cut off, the disabled lose their personal access tokens) or "
          "`dismiss-deletions` (Keycloak was wrong, and is fixed: the disabled's tokens work again); the "
          "identity.sync.breaker.tripped log line and `inkcast identity trips` name the pass.",
          "identity", for_="0m"),
    # E3.F3.6.S5: an engine error is a 503 for the person asking (fail closed), so any sustained rate pages.
    Alert("InkcastAuthzErrors", 'sum(rate(inkcast_authz_checks_total{result="error"}[5m])) > 0', "critical",
          "The authorization engine is failing; requests are refused (503)",
          "Permission checks have been failing for 5 minutes ({{ $value | humanize }}/s). Every failed check is a 503 "
          "for the caller: nothing is granted without an answer. OpenFGA is down or unreachable, the pinned model id "
          "is wrong, or the preshared key was rotated on one side only (authz.error in the logs says which).",
          "platform"),
)

SLOS: tuple[Slo, ...] = (
    Slo("api-availability", "Requests to the API and MCP endpoints that do not fail with a 5xx.", 0.999,
        f'inkcast_http_request_duration_seconds_count{{code!="5xx",{_API_ROUTES}}}',
        f'inkcast_http_request_duration_seconds_count{{{_API_ROUTES}}}', "publishing-api"),
    Slo("api-latency", "API and MCP requests answered within one second.", 0.99,
        f'inkcast_http_request_duration_seconds_bucket{{le="1",{_API_ROUTES}}}',
        f'inkcast_http_request_duration_seconds_count{{{_API_ROUTES}}}', "publishing-api"),
    Slo("publish-success", "Publish and update attempts the platform accepted (retries and recoveries are not attempts).",
        0.99, 'inkcast_publish_total{status=~"published|draft|unlisted|updated"}',
        'inkcast_publish_total{status=~"published|draft|unlisted|updated|failed|unconfirmed|update_failed"}',
        "scheduler-worker"),
    Slo("schedule-timeliness", "Scheduled posts claimed within two minutes of their time.", 0.99,
        'inkcast_publish_lag_seconds_bucket{le="120"}', 'inkcast_publish_lag_seconds_count', "scheduler-worker"),
)


def slo_rules(slo: Slo) -> tuple[list[dict], list[dict]]:
    """(recording rules, alert rules) for one SLO."""
    budget = 1 - slo.objective
    records = [{"record": f"inkcast_slo:{slo.key}:error_ratio_{w}", "expr": slo.error_ratio(w),
                "labels": {"slo": slo.name, "service": slo.service}} for w in WINDOWS]
    records.append({"record": f"inkcast_slo:{slo.key}:objective", "expr": str(slo.objective),
                    "labels": {"slo": slo.name, "service": slo.service}})
    alerts = []
    for long, short, burn, severity in BURN_WINDOWS:
        threshold = f"{burn} * {budget:.6g}"
        alerts.append({
            "alert": f"InkcastSloBurn{''.join(p.capitalize() for p in slo.name.split('-'))}",
            "expr": (f"inkcast_slo:{slo.key}:error_ratio_{long} > ({threshold})"
                     f" and inkcast_slo:{slo.key}:error_ratio_{short} > ({threshold})"),
            "for": "2m",
            "labels": {"severity": severity, "team": "inkcast", "service": slo.service, "slo": slo.name,
                       "burn_rate": str(burn), "long_window": long, "short_window": short},
            "annotations": {
                "summary": f"SLO {slo.name} burns its error budget {burn}x over {long}",
                "description": (f"{slo.description} Objective {slo.objective:.3%} over {slo.window_days} days; the "
                                f"error ratio over {long} and {short} both exceed {burn}x the budget "
                                f"({budget:.3%}). At this rate the monthly budget is gone in "
                                f"{slo.window_days * 24 / burn:.0f} hours."),
                "runbook_url": slo.runbook,
            },
        })
    return records, alerts


def prometheus_rule(name: str = "inkcast") -> dict:
    """The PrometheusRule resource with every alert, SLO recording rule and burn-rate alert."""
    groups = [{"name": "inkcast.alerts", "rules": [a.rule() for a in ALERTS]}]
    for slo in SLOS:
        records, alerts = slo_rules(slo)
        groups.append({"name": f"inkcast.slo.{slo.name}", "interval": "1m", "rules": records + alerts})
    return {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/name": "inkcast", "app.kubernetes.io/part-of": "inkcast",
                                              "role": "alert-rules"}},
        "spec": {"groups": groups},
    }


def all_alert_names() -> list[str]:
    names = [a.name for a in ALERTS]
    for slo in SLOS:
        names += [r["alert"] for r in slo_rules(slo)[1]]
    return names


__all__ = ["ALERTS", "Alert", "BURN_WINDOWS", "RUNBOOK_BASE", "SEVERITIES", "SLOS", "Slo", "all_alert_names",
           "prometheus_rule", "slo_rules"]
