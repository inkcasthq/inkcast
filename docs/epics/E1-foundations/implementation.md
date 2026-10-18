# E1 Platform foundations: implementation

## What was built

| Area | Files |
|---|---|
| Service model | `libs/service.py` (`ServiceSpec`, CamelCase ↔ kebab names), `launcher.py` (registry, `resolve`, config gate, servers and workers, signals) |
| Composition | `libs/http/app.py` (`compose`: `/`, `auto`, explicit prefix; probes; metrics app), `libs/runtime.py` (`Context`, metrics) |
| Services | `services/identity`, `services/publishing` (REST, MCP, sync), `services/connectors`, `services/scheduler`, `services/gateway` |
| Shared libraries | `libs/config.py`, `libs/secrets.py`, `libs/observability.py` (Redactor, `service` log field), `libs/auth`, `libs/db/*`, `libs/content`, `libs/platforms`, `libs/contracts/*`, `libs/http/{rest,docs,pages}.py`, `libs/pipeline.py` |
| Compatibility | `inkcast/{config,storage,story,models,publisher,observability,api,service,server}.py`, `inkcast/platforms`, `inkcast/auth`: aliases to the new modules |
| Manifests | `scripts/gen-manifests.py` generates `deploy/kustomize/components/{split-services,gateway-api}`; `overlays/ci-split` |
| CI | `.github/workflows/ci.yml` kind job: single pod, then split services through the Gateway |
| Tests | `test_architecture.py`, `test_split_mode.py`, `test_libs.py`; the existing suites, unchanged apart from the `unconfirmed` rename and new API paths |

## Configuration added

| Variable | Default | Meaning |
|---|---|---|
| `INKCAST_APP_NAME` | `All` | CamelCase service name(s), comma-separated |
| `INKCAST_PATH_PREFIX` | `/` | `/`, `auto` (= `/<full-name>`) or a path |
| `INKCAST_UPSTREAM_<NAME>` | none | Gateway only: base URL per service, e.g. `INKCAST_UPSTREAM_PUBLISHING_API=http://inkcast-publishing-api:8787` |
| `INKCAST_OVERDUE_GRACE` | `600` | seconds past due before a scheduled post counts as overdue (gauge and alert) |

## Data

- A new `heartbeats` table.
- A one-line data migration renames post status `unknown` to `unconfirmed`. It runs on start-up and is idempotent.
- Nothing else changed shape.

## Rollout and rollback

- **Rollout:** the default (`All`, prefix `/`) behaves like v0.1, so an existing deployment upgrades in place. The base Deployment still passes `args: ["all"]`. To split, add `components/split-services` to an overlay.
- **Rollback:** deploy the previous digest. The only data change (the status rename) is harmless to v0.1, which shows the status text as-is.

## Verification

- `PYTHONPATH=src:tests python -m unittest discover -s tests`: 80 tests, all passing. The end-to-end suites run once in a single process and again split behind the Gateway.
- `ruff check src tests scripts`: clean. `python scripts/gen-manifests.py --check`: current.
- kind (CI): both overlays roll out under Pod Security `restricted`, and the split smoke test passes through the Gateway.

## Deviations from the plan

- `TokenVerifier` has only the `db` adapter. RFC 7662 introspection is deferred until services stop sharing the store (E3). With a shared store it would only add a network hop.
- "Publish now" still sends inside the API request, as in v0.1, so that `inkcast_publish` can return the URL at once. Moving it onto the queue (and the scheduler) is story E7.F7.5.

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)
