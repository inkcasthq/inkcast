# Scheduler: separate worker and notifications

labels: scheduler

Run the scheduler as its own process (`inkcast worker`), claiming with `SELECT … FOR UPDATE SKIP LOCKED`.

- notifications on published/failed: email and outgoing webhooks (signed)
- metrics: queue depth, failures per platform

**Done when:** two workers never send the same post (tested).
