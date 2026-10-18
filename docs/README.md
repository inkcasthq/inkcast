# Inkcast documentation

**Start here:** [Bootstrap](bootstrap.md) — organisations, repository settings, CLA, legal, business set-up, platform developer accounts, first deployment.

> **Licensing in one line:** source-available ([PolyForm Shield](../LICENSE)). Free for your own content; agencies, monetised services and embedding need a [commercial licence](../legal/COMMERCIAL-LICENSE.md). [Details](../LICENSING.md).

## Product
- [Product spec](product-spec.md): vision, users, identity and accounts (Keycloak), phases.
- [Social platforms epic](design/social-platforms-epic.md): X, Instagram, TikTok design and sequencing.
- [Research: publishing to TikTok, X and Instagram](research/social-platforms-2026-10.md): official-docs findings, as of 2026-10-18.

## Using Inkcast
- [Publishing from a Git repository](git-publishing.md): tokens, the GitHub Action, `inkcast sync`, what each result means.

## Engineering
- [Architecture](architecture.md): services from one codebase, layers, sign-in flow, security model, cloud-native design, CNCF building blocks.
- [Contributor map](contributor-map.md): where a change goes, the four rules tests enforce, recipes (endpoint, platform, service, setting).
- [Definition of done](definition-of-done.md): for every story and epic, including design notes, mockups, implementation docs and independent review.
- [Epics](epics/README.md): design, stories, implementation and review per epic. Backlog as code in [backlog/](backlog/).
- [Architecture decision records](adr/README.md)
- [Secrets](reference/secrets.md): sources, the provider (TTL, single-flight, stale-on-error), naming, redaction, posture checks.
- [CI/CD](cicd.md): pipeline, supply chain, environments and promotion, GitHub plan notes.
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
