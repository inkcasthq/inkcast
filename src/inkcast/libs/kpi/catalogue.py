# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The KPI catalogue as code: every number Inkcast shows to a customer, a team, the owner or an operator,
with where it comes from, how it is aggregated, who may see it and which service owns it.

Four layers (docs/design/observability-and-kpis.md):

=============  ===========================================================  =====================
Layer          Who reads it                                                 Source of truth
=============  ===========================================================  =====================
system         operators (Grafana/Perses, alerts)                           Prometheus metrics
application    the owner and operators (admin portal, E6)                   events, DB aggregates,
                                                                            billing events (E5)
customer       an organization or workspace, on its own page                event rollups per scope
team           members of an organization, about each other                 event rollups per scope
                                                                            with a ``member`` dim
=============  ===========================================================  =====================

Rules the tests enforce:

* a ``built`` KPI names a producer that exists: a registered Prometheus metric, a rollup rule for its
  event, or a ``KpiStore`` reader;
* a ``planned`` KPI names the epic that delivers it and the event it will come from;
* per-customer numbers are never Prometheus labels: ``customer`` and ``team`` KPIs come from rollups;
* every customer or team KPI carries the OpenFGA relation that gates it (``can_view_usage``,
  ``can_view_audit``); ``self`` means "about the caller only" and ``operator`` means the admin role.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Layer = Literal["system", "application", "customer", "team"]
Source = Literal["prometheus", "events", "db", "billing"]
Visibility = Literal["operator", "owner", "can_view_usage", "can_view_audit", "self"]


@dataclass(frozen=True)
class Kpi:
    name: str
    layer: Layer
    unit: str                      # "posts", "ratio", "seconds", "requests/s", "USD"...
    aggregation: str               # sum | ratio | distinct | p95 | gauge | rate
    source: Source
    visibility: Visibility
    owner: str                     # the service that produces the number
    description: str
    producer: str = ""             # metric name, event type(s) or KpiStore reader, depending on source
    dims: tuple[str, ...] = ()     # dimensions a rollup keeps (small, closed sets only)
    numerator: str = ""            # ratio KPIs: the KPIs they are computed from at read time
    denominator: tuple[str, ...] = ()
    status: Literal["built", "planned"] = "built"
    epic: str = ""                 # planned KPIs: who delivers them
    tags: tuple[str, ...] = field(default_factory=tuple)


_SYS = "system"
_APP = "application"
_CUS = "customer"
_TEAM = "team"

CATALOGUE: tuple[Kpi, ...] = (
    # ---- system / infrastructure: RED and USE, from Prometheus (no per-customer labels, ever) ----
    Kpi("http_request_rate", _SYS, "requests/s", "rate", "prometheus", "operator", "every HTTP service",
        "Requests per second by route template, method and status class.", "inkcast_http_request_duration_seconds"),
    Kpi("http_error_ratio", _SYS, "ratio", "ratio", "prometheus", "operator", "every HTTP service",
        "5xx responses over all responses, by route template.", "inkcast_http_request_duration_seconds"),
    Kpi("http_latency_p95", _SYS, "seconds", "p95", "prometheus", "operator", "every HTTP service",
        "95th percentile request duration from the histogram buckets.", "inkcast_http_request_duration_seconds"),
    Kpi("gateway_upstream_errors", _SYS, "requests/s", "rate", "prometheus", "operator", "Gateway",
        "Proxied requests that failed or returned 5xx, by upstream.", "inkcast_gateway_requests_total"),
    Kpi("publish_attempt_rate", _SYS, "attempts/s", "rate", "prometheus", "operator", "SchedulerWorker, PublishingApi",
        "Publish attempts by platform and outcome.", "inkcast_publish_total"),
    Kpi("publish_call_latency_p95", _SYS, "seconds", "p95", "prometheus", "operator", "SchedulerWorker, PublishingApi",
        "95th percentile of one platform publish call, by platform.", "inkcast_publish_duration_seconds"),
    Kpi("queue_depth", _SYS, "posts", "gauge", "prometheus", "operator", "SchedulerWorker",
        "Scheduled posts due now (the KEDA scaler input).", "inkcast_posts_due"),
    Kpi("queue_overdue", _SYS, "posts", "gauge", "prometheus", "operator", "SchedulerWorker",
        "Due posts older than INKCAST_OVERDUE_GRACE: the scheduler is behind or stopped.", "inkcast_posts_overdue"),
    Kpi("queue_head_lag", _SYS, "seconds", "gauge", "prometheus", "operator", "SchedulerWorker",
        "Age of the oldest due post still waiting (head-of-line lag).", "inkcast_posts_oldest_due_age_seconds"),
    Kpi("schedule_lag_p95", _SYS, "seconds", "p95", "prometheus", "operator", "SchedulerWorker",
        "95th percentile of how late posts were claimed after their run_at, by platform.", "inkcast_publish_lag_seconds"),
    Kpi("scheduler_tick_rate", _SYS, "ticks/s", "rate", "prometheus", "operator", "SchedulerWorker",
        "Completed scheduler ticks; zero means the loop is stuck.", "inkcast_scheduler_ticks_total"),
    Kpi("outbox_consumer_lag", _SYS, "seconds", "gauge", "prometheus", "operator", "InsightsService",
        "Age of the oldest domain event a consumer has not handled.", "inkcast_outbox_oldest_age_seconds"),
    Kpi("outbox_pending", _SYS, "events", "gauge", "prometheus", "operator", "InsightsService",
        "Domain events waiting per consumer.", "inkcast_outbox_pending"),
    Kpi("cache_hit_ratio", _SYS, "ratio", "ratio", "prometheus", "operator", "every service",
        "Cache hits over hits plus misses, by backend.", "inkcast_cache_requests_total"),
    Kpi("cache_error_rate", _SYS, "errors/s", "rate", "prometheus", "operator", "every service",
        "Cache backend failures (requests fell back to in-process behaviour).", "inkcast_cache_errors_total"),
    Kpi("ratelimit_rejections", _SYS, "requests/s", "rate", "prometheus", "operator", "IdentityService, ConnectorService",
        "Unauthenticated writes refused with 429, by limit.", "inkcast_ratelimit_total"),
    Kpi("sync_rate", _SYS, "results/s", "rate", "prometheus", "operator", "PublishingApi",
        "Git publishing results by platform and action.", "inkcast_sync_total"),

    # ---- application / Inkcast-internal: platform health across every customer ----
    Kpi("platform_publish_success_rate", _APP, "ratio", "ratio", "events", "operator", "InsightsService",
        "Across all customers: published over published + failed + unconfirmed, per platform.",
        numerator="posts_published", denominator=("posts_published", "posts_failed", "posts_unconfirmed"),
        producer="aggregate", dims=("platform",)),
    Kpi("platform_unconfirmed_rate", _APP, "ratio", "ratio", "events", "operator", "InsightsService",
        "Unconfirmed sends over all attempts, per platform: the duplicate-risk signal.",
        numerator="posts_unconfirmed", denominator=("posts_published", "posts_failed", "posts_unconfirmed"),
        producer="aggregate", dims=("platform",)),
    Kpi("active_publishers", _APP, "users", "distinct", "db", "operator", "InsightsService",
        "Distinct users who published at least once in the period (activation and retention input).",
        producer="distinct_scopes"),
    Kpi("signups", _APP, "users", "sum", "events", "operator", "IdentityService",
        "New Inkcast users created at first login.", producer="user.created", status="planned", epic="E9"),
    Kpi("activation_rate", _APP, "ratio", "ratio", "events", "operator", "InsightsService",
        "Users who connected a platform and published within 7 days of sign-up, over sign-ups.",
        producer="user.created,post.published", status="planned", epic="E9"),
    Kpi("mrr", _APP, "USD", "gauge", "billing", "owner", "SubscriptionService",
        "Monthly recurring revenue from active subscriptions (annual plans divided by 12).",
        producer="subscription.changed", status="planned", epic="E5"),
    Kpi("arr", _APP, "USD", "gauge", "billing", "owner", "SubscriptionService",
        "MRR times 12.", producer="subscription.changed", status="planned", epic="E5"),
    Kpi("churn_rate", _APP, "ratio", "ratio", "billing", "owner", "SubscriptionService",
        "Organizations that cancelled in the month over those active at its start.",
        producer="subscription.canceled", status="planned", epic="E5"),
    Kpi("net_revenue_retention", _APP, "ratio", "ratio", "billing", "owner", "SubscriptionService",
        "MRR today from the cohort active 12 months ago, over that cohort's MRR then.",
        producer="subscription.changed", status="planned", epic="E5"),
    Kpi("arpa", _APP, "USD", "ratio", "billing", "owner", "SubscriptionService",
        "MRR over paying organizations.", producer="subscription.changed", status="planned", epic="E5"),
    Kpi("credits_spent", _APP, "credits", "sum", "events", "operator", "SubscriptionService",
        "Paid-platform sends and AI assists charged to the credits wallet (X costs, issue 21).",
        producer="credits.spent", status="planned", epic="E8"),
    Kpi("infra_cost", _APP, "USD", "gauge", "prometheus", "owner", "operators",
        "Cluster cost allocated to the inkcast namespace (OpenCost).", producer="opencost",
        status="planned", epic="E7"),

    # ---- customer-facing, per organization and workspace (rollups by scope; gated by OpenFGA) ----
    Kpi("posts_published", _CUS, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Posts a platform accepted (published, draft or unlisted).", producer="post.published",
        dims=("platform", "status")),
    Kpi("posts_scheduled", _CUS, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Posts queued for later (retries are counted in posts_retried, not here).", producer="post.scheduled",
        dims=("platform",)),
    Kpi("posts_retried", _CUS, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Sends that were re-queued after a transient platform error.", producer="post.scheduled", dims=("platform",)),
    Kpi("posts_failed", _CUS, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Posts that failed for good and wait for a person.", producer="post.failed", dims=("platform", "reason")),
    Kpi("posts_unconfirmed", _CUS, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Posts a platform may or may not have published.", producer="post.unconfirmed", dims=("platform",)),
    Kpi("posts_recovered", _CUS, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Posts resolved after an interrupted send.", producer="post.recovered", dims=("platform", "status")),
    Kpi("posts_cancelled", _CUS, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Posts a person cancelled: scheduled ones before they went out, and recovering or unconfirmed ones whose send "
        "the platform never confirmed.", producer="post.cancelled", dims=("platform",)),
    Kpi("publish_success_rate", _CUS, "ratio", "ratio", "events", "can_view_usage", "InsightsService",
        "Published over published + failed + unconfirmed, per platform.", producer="aggregate",
        numerator="posts_published", denominator=("posts_published", "posts_failed", "posts_unconfirmed"),
        dims=("platform",)),
    Kpi("posts_on_time", _CUS, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Scheduled posts sent within ON_TIME_SECONDS of their time.", producer="post.published", dims=("platform",)),
    Kpi("posts_late", _CUS, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Scheduled posts sent later than ON_TIME_SECONDS after their time.", producer="post.published",
        dims=("platform",)),
    Kpi("on_time_rate", _CUS, "ratio", "ratio", "events", "can_view_usage", "InsightsService",
        "On-time over on-time + late scheduled sends.", producer="aggregate",
        numerator="posts_on_time", denominator=("posts_on_time", "posts_late"), dims=("platform",)),
    Kpi("schedule_lag_seconds", _CUS, "seconds", "sum", "events", "can_view_usage", "InsightsService",
        "Total lag of scheduled sends (divide by posts_on_time + posts_late for the mean).",
        producer="post.published", dims=("platform",)),
    Kpi("sync_results", _CUS, "files", "sum", "events", "can_view_usage", "InsightsService",
        "Git publishing outcomes per (file, platform): create, update, unchanged, stale, skipped, failed.",
        producer="sync.result", dims=("platform", "action")),
    Kpi("approval_cycle_seconds", _CUS, "seconds", "sum", "events", "can_view_usage", "InsightsService",
        "Time from a post's approval request to its approval (E4 approvals).", producer="post.approved",
        status="planned", epic="E4"),
    Kpi("channel_health", _CUS, "channels", "gauge", "events", "can_view_usage", "ConnectorService",
        "Connected channels by state: healthy, degraded, reauth_required, revoked.", producer="channel.state",
        status="planned", epic="E7"),
    Kpi("usage_vs_plan", _CUS, "ratio", "gauge", "db", "can_view_usage", "SubscriptionService",
        "Posts, channels and seats used over the plan's limits (libs/entitlements counters).",
        producer="entitlements", status="planned", epic="E5"),
    Kpi("engagement", _CUS, "events", "sum", "events", "can_view_usage", "InsightsService",
        "Views, reactions and comments read back from platform APIs.", producer="engagement.sampled",
        status="planned", epic="E8"),

    # ---- team, inside an organization: the same facts with a member dimension ----
    Kpi("member_posts_published", _TEAM, "posts", "sum", "events", "can_view_usage", "InsightsService",
        "Posts published per member of a workspace.", producer="post.published", dims=("member", "platform")),
    Kpi("member_sync_results", _TEAM, "files", "sum", "events", "can_view_usage", "InsightsService",
        "Git publishing outcomes per member (the token's owner).", producer="sync.result", dims=("member", "action")),
    Kpi("member_approval_latency", _TEAM, "seconds", "sum", "events", "can_view_usage", "InsightsService",
        "Time a reviewer takes to approve, per reviewer (reviewer load).", producer="post.approved",
        dims=("member",), status="planned", epic="E4"),
    Kpi("member_actions", _TEAM, "actions", "sum", "events", "can_view_audit", "InsightsService",
        "Audited state changes per member (contribution, from the audit log).", producer="audit.recorded",
        dims=("member", "action"), status="planned", epic="E3"),
)

BY_NAME: dict[str, Kpi] = {k.name: k for k in CATALOGUE}
ON_TIME_SECONDS = 120.0  # a scheduled post sent within two minutes of its time is on time (six ticks)


def built() -> list[Kpi]:
    return [k for k in CATALOGUE if k.status == "built"]


def visible_to_customers() -> list[Kpi]:
    return [k for k in CATALOGUE if k.layer in ("customer", "team") and k.status == "built"]


__all__ = ["BY_NAME", "CATALOGUE", "Kpi", "ON_TIME_SECONDS", "built", "visible_to_customers"]
