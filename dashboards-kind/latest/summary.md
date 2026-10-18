# Dashboard check: PASS

Grafana http://localhost:3000, Prometheus http://localhost:9091, namespace `inkcast`, 16 panels.

| Panel | Required | Data | Errors |
|---|---|---|---|
| Pods scraped | yes | yes |  |
| Version and plug-ins | yes | yes |  |
| Posts due and overdue (count) |  | yes |  |
| Scheduler ticks (per second) | yes | yes |  |
| Schedule lag p95 (seconds) | yes | yes |  |
| Oldest due post age (seconds) |  | yes |  |
| Publish outcomes (per second, stacked) | yes | yes |  |
| Publish call p95 (seconds) | yes | yes |  |
| Publish success SLO: error ratio over 1 h |  | yes |  |
| Schedule timeliness SLO: error ratio over 1 h |  | no |  |
| Requests by status class (per second, stacked) | yes | yes |  |
| Request p95 by route (seconds) |  | no |  |
| API availability SLO: error ratio over 1 h |  | yes |  |
| Rate-limited writes (per second) |  | no |  |
| Outbox lag (seconds) |  | yes |  |
| Cache hit ratio (share) |  | no |  |
