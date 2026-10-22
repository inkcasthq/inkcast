# Inkcast documentation

**Start here:** [Bootstrap](bootstrap.md) — organisations, repository settings, CLA, legal, business set-up, platform developer accounts, first deployment.

> **Licensing in one line:** source-available ([PolyForm Shield](../LICENSE)). Free for your own content; agencies, monetised services and embedding need a [commercial licence](../legal/COMMERCIAL-LICENSE.md). [Details](../LICENSING.md).

## Product
- [Product spec](product-spec.md): vision, users, identity and accounts (Keycloak), phases.
- [Social platforms epic](design/social-platforms-epic.md): X, Instagram, TikTok design and sequencing.
- [Research: publishing to TikTok, X and Instagram](research/social-platforms-2026-10.md): official-docs findings, as of 2026-10-18.
- [Research: OpenFGA behaviour for our model and engines](research/openfga-2026-10.md): self-referential usersets, the new check engine, limits; pinned v1.22.0, as of 2026-10-22.

## Product strategy
- [Tiers, subscriptions and entitlements (E10)](epics/E10-tiers-and-billing/design.md): Free, Personal, Team, Business, Enterprise; Agency add-on; billing provider bake-off; offline licences.

## Using Inkcast
- [Publishing from a Git repository](git-publishing.md): tokens, the GitHub Action, `inkcast sync`, what each result means.

## Engineering
- [Architecture](architecture.md): services from one codebase, layers, sign-in flow, security model, cloud-native design, CNCF building blocks.
- [Contributor map](contributor-map.md): where a change goes, the four rules tests enforce, recipes (endpoint, platform, service, setting).
- [Definition of done](definition-of-done.md): for every story and epic, including design notes, mockups, implementation docs and independent review.
- [Epics](epics/README.md): design, stories, implementation and review per epic. Backlog as code in [backlog/](backlog/).
- [Architecture decision records](adr/README.md)
- [Authorization](authorization.md): Keycloak for who you are, OpenFGA for what you may do, the database for business state; the model in `authz/` with its tests.
- [Secrets](reference/secrets.md): sources, the provider (TTL, single-flight, stale-on-error), naming, redaction, posture checks.
- [Observability, KPIs and the data stack](design/observability-and-kpis.md): metrics per layer, the KPI catalogue, the outbox, Valkey/Redis, alerts and SLOs, dashboards. Runbooks in [runbooks/alerts.md](runbooks/alerts.md).
- [Backup and restore](runbooks/backup-restore.md): daily SQLite backups, the restore script and the drill, until PostgreSQL on CloudNativePG ([ADR-0014](adr/0014-postgresql-on-cloudnativepg.md)).
- [CI/CD](cicd.md): pipeline, supply chain, environments and promotion, GitHub plan notes.
- [Screenshots](screenshots.md): Inkcast in use, from consent to connecting a platform, publishing, tokens, the API reference and deleting an account.
- [End-to-end walkthrough](walkthrough.md): the whole journey driven for real by an MCP client and a browser, pictured and checked by CI; published to the `ci-captures` branch.
- [Writing a platform plug-in](writing-a-platform-plugin.md)
- API reference: served by every deployment at `/api` (Swagger UI) and `/api/redoc`. Screenshots: [Swagger UI](images/api-swagger-ui.png), [ReDoc](images/api-redoc.png).

## Roadmap
- [Epics and backlog](epics/README.md): file the backlog with `scripts/backlog/file.py inkcasthq/inkcast`.
- [Issues](issues/): earlier ready-to-file issues. File them with `scripts/file-issues.sh inkcasthq/inkcast`.

## Organisation
- [GitHub org profiles](../org-profiles/README.md) for `inkcasthq` and `getinkcast`.

## Legal and licensing
- [Licensing model](../LICENSING.md): free versus commercial use, definitions, quick check.
- [Legal documents](../legal/README.md): licence, additional permissions, commercial licences, CLAs, status.
- [Trademarks](../TRADEMARKS.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Notices](../NOTICE)
