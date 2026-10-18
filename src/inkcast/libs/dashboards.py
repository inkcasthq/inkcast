# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The operator dashboard as code, to the owner's /dashboard standard: one section per question a reader
asks, few metrics, the chart chosen by the kind of data, one fixed colour per identity from the Tableau 10
palette, words on every panel, and only panel kinds that convert to Perses (timeseries, stat, table).

``scripts/gen-dashboards.py`` writes the Grafana JSON and a ConfigMap for the sidecar; the Perses form is
generated from that JSON with the kit's ``perses-dashboard`` command (not in this repository). Rules the
tests keep: a description on every panel, the legend below, line width 1 with a 10% shade, a fixed colour
per query, ``[5m]`` rate windows stated in descriptions, and no panel kind Perses 0.54 turns into a placeholder.
"""

from __future__ import annotations

from typing import Any

# Section 4 of the standard: identity colours in order, then the two state colours.
BLUE, RED, YELLOW, PURPLE, GREEN, ORANGE = "#4e79a7", "#e15759", "#edc948", "#b07aa1", "#59a14f", "#f28e2b"
# The palette's grey, for "No data" only: a threshold's base step is what Grafana paints when there is no value, so
# it is never green (an empty panel would look healthy) or red (it would look broken). From group-sync-dashboard.
NEUTRAL = "#bab0ac"
IDENTITY = (BLUE, RED, YELLOW, PURPLE)
STYLES = ("solid", "dash", "dot")  # first line solid, second dashed, third dotted
RATE = "5m"
PANEL_KINDS = {"timeseries", "stat", "table"}

def _query(ref: str, expr: str, legend: str) -> dict[str, Any]:
    return {"refId": ref, "expr": expr, "legendFormat": legend, "range": True}


def _override(ref: str, color: str, style: str = "solid", fill: int = 10) -> dict[str, Any]:
    props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}},
             {"id": "custom.fillOpacity", "value": fill}]
    if style != "solid":
        props.append({"id": "custom.lineStyle", "value": {"fill": style, "dash": [10, 10] if style == "dash" else [0, 10]}})
    return {"matcher": {"id": "byFrameRefID", "options": ref}, "properties": props}


def timeseries(title: str, description: str, queries: list[tuple[str, str]], unit: str = "short", *,
               colors: tuple[str, ...] = IDENTITY, stacked: bool = False, max_: float | None = None) -> dict[str, Any]:
    """``queries``: (expr, legend) in identity order; each gets the next colour and line style."""
    refs = [chr(ord("A") + i) for i in range(len(queries))]
    custom: dict[str, Any] = {"lineWidth": 1, "fillOpacity": 10 if not stacked else 30, "axisSoftMin": 0,
                              "stacking": {"mode": "normal" if stacked else "none"}}
    std: dict[str, Any] = {"unit": unit, "custom": custom, "min": 0}
    if max_ is not None:
        std["max"] = max_
    return {
        "type": "timeseries", "title": title, "description": description,
        "targets": [_query(r, e, legend) for r, (e, legend) in zip(refs, queries)],
        "fieldConfig": {"defaults": std,
                        "overrides": [_override(r, colors[i % len(colors)], STYLES[i % len(STYLES)] if not stacked else "solid",
                                                30 if stacked else 10) for i, r in enumerate(refs)]},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def stat(title: str, description: str, expr: str, unit: str = "short", *, thresholds: list[tuple[float, str]] | None = None,
         legend: str = "", at_zero: str = GREEN) -> dict[str, Any]:
    """A number. Its colour: NEUTRAL with no value, ``at_zero`` from 0 (every value here is 0 or more), then each of
    ``thresholds`` (value, colour) upwards."""
    steps = ([{"color": NEUTRAL, "value": None}, {"color": at_zero, "value": 0}]
             + [{"color": c, "value": v} for v, c in (thresholds or [])])
    return {
        "type": "stat", "title": title, "description": description,
        "targets": [_query("A", expr, legend or "__auto")],
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": "value",
                    "graphMode": "none", "textMode": "value"},
    }


def table(title: str, description: str, expr: str, columns: dict[str, str]) -> dict[str, Any]:
    """Facts as rows: an instant query (a range query gives one series per row and Grafana shows a series picker,
    not a table), one row per series, the labels in `columns` (label -> header) in that order, Time and Value
    hidden."""
    return {
        "type": "table", "title": title, "description": description,
        "targets": [{"refId": "A", "expr": expr, "legendFormat": "__auto", "format": "table", "instant": True,
                     "range": False}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"showHeader": True},
        "transformations": [{"id": "organize", "options": {
            "excludeByName": {"Time": True, "Value": True},
            "indexByName": {label: i for i, label in enumerate(columns)},
            "renameByName": dict(columns)}}],
    }


def row(title: str) -> dict[str, Any]:
    return {"type": "row", "title": title, "collapsed": False, "panels": []}


def _layout(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two panels per row, 12 wide and 8 high; a row header spans the width. Ids follow the order."""
    y, x = 0, 0
    for i, p in enumerate(items, 1):
        p["id"] = i
        if p["type"] == "row":
            if x:
                y, x = y + 8, 0
            p["gridPos"] = {"h": 1, "w": 24, "x": 0, "y": y}
            y += 1
            continue
        p["gridPos"] = {"h": 8, "w": 12, "x": x, "y": y}
        x += 12
        if x >= 24:
            x, y = 0, y + 8
    return items


def platform_dashboard() -> dict[str, Any]:
    ns = 'namespace="$namespace"'
    s = f"{ns}, job=~\".*inkcast.*\""
    panels = [
        row("Is Inkcast up?"),
        stat("Pods scraped", "up for every Inkcast target in the namespace (last value): how many answer. Green from 1; "
             "red at 0, when none does (InkcastTargetDown pages). Grey 'No data' means nothing is scraped at all: check "
             "the ServiceMonitor.",
             f'sum(up{{{s}}})', at_zero=RED, thresholds=[(0.5, GREEN)]),
        table("Version and plug-ins", "inkcast_build_info: the version and the platform plug-ins each pod runs (facts, so a "
              "table). Every row should match after a rollout.", f'max by (pod, version, platforms) (inkcast_build_info{{{ns}}})',
              {"pod": "Pod", "version": "Version", "platforms": "Plug-ins"}),
        row("Is the queue moving?"),
        timeseries("Posts due and overdue (count)", "inkcast_posts_due (blue): scheduled posts due now. inkcast_posts_overdue "
                   "(red, dashed): due for longer than INKCAST_OVERDUE_GRACE. Gauges, as they are. Normal: due near 0 between "
                   "ticks, overdue at 0. Overdue above 0 pages (InkcastPostsOverdue).",
                   [(f'max(inkcast_posts_due{{{ns}}})', "due"), (f'max(inkcast_posts_overdue{{{ns}}})', "overdue")]),
        timeseries("Scheduler ticks (per second)", f"rate(inkcast_scheduler_ticks_total[{RATE}]): completed ticks. Normal: 1/20 "
                   f"= 0.05 per second with the default interval. Zero for 10 minutes pages (InkcastSchedulerStalled).",
                   [(f'sum(rate(inkcast_scheduler_ticks_total{{{ns}}}[{RATE}]))', "ticks")], unit="ops"),
        timeseries("Schedule lag p95 (seconds)", f"histogram_quantile(0.95, inkcast_publish_lag_seconds) over [{RATE}], by "
                   f"platform: how late scheduled posts were claimed. Normal: under 20 s (one tick). The SLO allows 120 s. "
                   f"Empty: nothing was due in the window.",
                   [(f'histogram_quantile(0.95, sum by (le) (rate(inkcast_publish_lag_seconds_bucket{{{ns}, platform="medium"}}[{RATE}])))',
                     "medium"),
                    (f'histogram_quantile(0.95, sum by (le) (rate(inkcast_publish_lag_seconds_bucket{{{ns}, platform="devto"}}[{RATE}])))',
                     "devto")], unit="s"),
        timeseries("Oldest due post age (seconds)", "inkcast_posts_oldest_due_age_seconds: head-of-line lag, as it is. Normal: "
                   "sawtooth under 20 s. Over 300 s for 10 minutes warns (InkcastQueueHeadLagHigh).",
                   [(f'max(inkcast_posts_oldest_due_age_seconds{{{ns}}})', "oldest due")], unit="s"),
        row("Are publishes succeeding?"),
        timeseries("Publish outcomes (per second, stacked)", f"rate(inkcast_publish_total[{RATE}]) by status, parts of a whole: "
                   f"published (green), failed (red), unconfirmed (orange), retry (yellow). Normal: all green. Any unconfirmed "
                   f"needs a person (InkcastUnconfirmedPublishes).",
                   [(f'sum(rate(inkcast_publish_total{{{ns}, status=~"published|draft|unlisted|updated"}}[{RATE}]))', "published"),
                    (f'sum(rate(inkcast_publish_total{{{ns}, status=~"failed|update_failed"}}[{RATE}]))', "failed"),
                    (f'sum(rate(inkcast_publish_total{{{ns}, status="unconfirmed"}}[{RATE}]))', "unconfirmed"),
                    (f'sum(rate(inkcast_publish_total{{{ns}, status="retry"}}[{RATE}]))', "retry")],
                   unit="ops", colors=(GREEN, RED, ORANGE, YELLOW), stacked=True),
        timeseries("Publish call p95 (seconds)", f"histogram_quantile(0.95, inkcast_publish_duration_seconds) over [{RATE}] by "
                   f"platform: one platform call. Normal: under 2 s. Near 30 s means timeouts, which end unconfirmed.",
                   [(f'histogram_quantile(0.95, sum by (le) (rate(inkcast_publish_duration_seconds_bucket{{{ns}, platform="medium"}}[{RATE}])))',
                     "medium"),
                    (f'histogram_quantile(0.95, sum by (le) (rate(inkcast_publish_duration_seconds_bucket{{{ns}, platform="devto"}}[{RATE}])))',
                     "devto")], unit="s"),
        stat("Publish success SLO: error ratio over 1 h", "inkcast_slo:publish_success:error_ratio_1h (recording rule). The "
             "objective is 99%, so the budget is 1%: green under 1%, orange under 6x (burning), red above 14.4x (paging).",
             'inkcast_slo:publish_success:error_ratio_1h', unit="percentunit", thresholds=[(0.01, ORANGE), (0.144, RED)]),
        stat("Schedule timeliness SLO: error ratio over 1 h", "inkcast_slo:schedule_timeliness:error_ratio_1h. Objective 99%: "
             "the share of scheduled posts claimed later than 120 s. Green under 1%.",
             'inkcast_slo:schedule_timeliness:error_ratio_1h', unit="percentunit", thresholds=[(0.01, ORANGE), (0.144, RED)]),
        row("Is the API healthy?"),
        timeseries("Requests by status class (per second, stacked)", f"rate(inkcast_http_request_duration_seconds_count[{RATE}]) "
                   f"by code on API and MCP routes: 2xx green, 4xx orange, 5xx red. Probes are not counted. Normal: mostly "
                   f"green; 4xx are clients (401s from MCP discovery are expected).",
                   [(f'sum(rate(inkcast_http_request_duration_seconds_count{{{ns}, code="2xx", route=~"/api/.*|/mcp"}}[{RATE}]))', "2xx"),
                    (f'sum(rate(inkcast_http_request_duration_seconds_count{{{ns}, code="4xx", route=~"/api/.*|/mcp"}}[{RATE}]))', "4xx"),
                    (f'sum(rate(inkcast_http_request_duration_seconds_count{{{ns}, code="5xx", route=~"/api/.*|/mcp"}}[{RATE}]))', "5xx")],
                   unit="reqps", colors=(GREEN, ORANGE, RED), stacked=True),
        timeseries("Request p95 by route (seconds)", f"histogram_quantile(0.95, inkcast_http_request_duration_seconds) over "
                   f"[{RATE}] for the three routes that call a platform or render: publish (blue), sync (red, dashed), "
                   f"preview (yellow, dotted). The SLO allows 1 s. Empty: no traffic on the route.",
                   [(f'histogram_quantile(0.95, sum by (le) (rate(inkcast_http_request_duration_seconds_bucket{{{ns}, route="/api/v1/posts", method="POST"}}[{RATE}])))', "publish"),
                    (f'histogram_quantile(0.95, sum by (le) (rate(inkcast_http_request_duration_seconds_bucket{{{ns}, route="/api/v1/stories:sync"}}[{RATE}])))', "sync"),
                    (f'histogram_quantile(0.95, sum by (le) (rate(inkcast_http_request_duration_seconds_bucket{{{ns}, route="/api/v1/preview"}}[{RATE}])))', "preview")],
                   unit="s"),
        stat("API availability SLO: error ratio over 1 h", "inkcast_slo:api_availability:error_ratio_1h: the share of API and "
             "MCP requests that returned 5xx. Objective 99.9%: green under 0.1%.",
             'inkcast_slo:api_availability:error_ratio_1h', unit="percentunit", thresholds=[(0.001, ORANGE), (0.0144, RED)]),
        timeseries("Rate-limited writes (per second)", f"rate(inkcast_ratelimit_total{{decision=\"limited\"}}[{RATE}]) by limit: "
                   f"429s on registration (blue), sign-in (red, dashed), tokens (yellow, dotted), connect (purple). Normal: 0. "
                   f"Sustained above 1/s is a loop or abuse (InkcastRateLimitSurge).",
                   [(f'sum(rate(inkcast_ratelimit_total{{{ns}, decision="limited", limit="register"}}[{RATE}]))', "register"),
                    (f'sum(rate(inkcast_ratelimit_total{{{ns}, decision="limited", limit="login"}}[{RATE}]))', "login"),
                    (f'sum(rate(inkcast_ratelimit_total{{{ns}, decision="limited", limit="tokens-new"}}[{RATE}]))', "tokens"),
                    (f'sum(rate(inkcast_ratelimit_total{{{ns}, decision="limited", limit="connect"}}[{RATE}]))', "connect")],
                   unit="reqps"),
        row("Are events and the cache flowing?"),
        timeseries("Outbox lag (seconds)", "inkcast_outbox_oldest_age_seconds per consumer, as it is: how old the oldest unhandled "
                   "domain event is. Normal: under the consumer interval (10 s). Over 600 s warns (InkcastOutboxConsumerLag); "
                   "KPIs are stale, nothing else.",
                   [(f'max(inkcast_outbox_oldest_age_seconds{{{ns}, consumer="insights"}})', "insights")], unit="s"),
        timeseries("Cache hit ratio (share)", f"hits / (hits + misses) from inkcast_cache_requests_total over [{RATE}], per backend. "
                   f"0 to 1. Empty: nothing read through the cache yet. Errors are on the alert "
                   f"InkcastCacheBackendErrors, not here.",
                   [(f'sum(rate(inkcast_cache_requests_total{{{ns}, backend="redis", result="hit"}}[{RATE}])) / clamp_min(sum(rate(inkcast_cache_requests_total{{{ns}, backend="redis"}}[{RATE}])), 1e-9)', "redis"),
                    (f'sum(rate(inkcast_cache_requests_total{{{ns}, backend="memory", result="hit"}}[{RATE}])) / clamp_min(sum(rate(inkcast_cache_requests_total{{{ns}, backend="memory"}}[{RATE}])), 1e-9)', "memory")],
                   unit="percentunit", max_=1),
    ]
    return {
        "uid": "inkcast-platform", "title": "Inkcast · platform health", "tags": ["inkcast"], "timezone": "utc",
        "schemaVersion": 39, "version": 1, "editable": False, "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": [{"name": "namespace", "type": "query", "datasource": {"type": "prometheus"},
                                 "query": "label_values(inkcast_build_info, namespace)", "refresh": 2,
                                 "current": {"text": "inkcast", "value": "inkcast"}}]},
        "panels": _layout(panels),
    }


__all__ = ["BLUE", "GREEN", "IDENTITY", "ORANGE", "PANEL_KINDS", "PURPLE", "RATE", "RED", "YELLOW", "platform_dashboard"]
