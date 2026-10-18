# ADR-0001: One codebase, many services

**Status:** accepted, 2026-10-18 · **Epic:** E1

## Context
Inkcast v0.1 was one process. The SaaS needs independently scalable and deployable parts (sign-in, the API, credential handling, the scheduler, later billing and admin), but a small team can't afford many repositories, images and release trains, and self-hosters want one container.

## Options
1. **Keep a monolith**: simple, but every part scales and fails together, and boundaries erode.
2. **One repository per service**: hard boundaries, but N pipelines, versions and contracts to keep in sync.
3. **One codebase, one image, services chosen at start-up**: boundaries enforced in code and tests, one build, any topology.

## Decision
Option 3.
- Each service is a `ServiceSpec` in `inkcast.services.<pkg>`.
- `INKCAST_APP_NAME` selects one or more services by **CamelCase** name (`PublishingApi`, `IdentityService,ConnectorService`). `All` runs everything in one process.
- The kebab-case full name (`publishing-api`) is derived from it. It is used for URL prefixes, Kubernetes names, metric labels and log fields.
- `INKCAST_PATH_PREFIX` serves a process at `/` or under `/<full-name>` (`auto`).
- Shared code lives in `inkcast.libs`. Tests forbid libs importing services, and services importing each other.

## Consequences
- **Upside:** one image and one digest to sign and promote; the same code runs as a laptop single process, a single pod, or split pods. Split mode is tested end to end on every build (`tests/test_split_mode.py`), so decoupling can't silently rot.
- **Shared database:** services still share one store, through per-domain repositories, each owned by one service (ADR-0003). True database-per-service comes when a service needs it, behind the same repository interfaces.
- **Image size:** the image contains every service's code. That's acceptable at this size; revisit if one service pulls in heavy dependencies.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
