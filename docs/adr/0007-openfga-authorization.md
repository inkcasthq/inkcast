# ADR-0007: OpenFGA for authorization, behind a pluggable engine

**Status:** accepted, 2026-10-18 · **Epics:** E3 (F3.5–F3.9) · **Design:** [docs/authorization.md](../authorization.md)

## Context
Teams, agencies and tiers need fine-grained, relationship-shaped permissions:
- a member of an agency edits a client's workspace;
- a reviewer approves but can't edit;
- publishing to a channel is its own right.

These must hold identically for web, mobile, REST, MCP and background jobs, and they must be testable by juniors.

## Options
1. **Role columns in our DB with hand-written checks.** Simple at first, but agencies, inheritance and custom roles turn it into ad-hoc code spread across services.
2. **Keycloak Authorization Services.** Puts resource policy in the IdP and couples every check to Keycloak.
3. **OPA or Cerbos.** Attribute policies, but relationships (org → workspace → channel/post, agency groups) still have to be fed in on every call.
4. **OpenFGA.** A Zanzibar-style relationship engine and a CNCF incubating project (since November 2025). The model is code with its own test format, and it offers `Check`, `BatchCheck` and `ListObjects`.

## Decision
**OpenFGA from day 1**, behind `PolicyEngine` in `inkcast.libs.authz`.

- **`OpenFgaEngine` (what the hosted service runs)** calls OpenFGA's REST API with `httpx2`, with no SDK dependency. Its credentials come from the `SecretsProvider`, and it **fails closed**.
- **`LocalEngine`** evaluates the compiled model (`authz/model.json`) in-process over tuples in the Inkcast DB. It is for tests, laptops and small air-gapped self-hosts. It is the **default** (`INKCAST_AUTHZ_ENGINE=local`), so a deployment without OpenFGA keeps working; production and staging refuse it unless `INKCAST_AUTHZ_ALLOW_LOCAL=true` states that this is a self-host that accepts it, and the hosted environments set it to `false` explicitly (staging since 2026-10-22: it rehearses production) (E3.F3.6.S4: nothing else in the configuration says "hosted").
- Both engines must pass `authz/model.fga.yaml`. CI runs `fga model test` (pinned CLI 0.8.2, checksum-verified), and the Python suite runs the same YAML against `LocalEngine`.
- **One enforcement path:** `authz.require(principal, relation, ref, scope=…)` = token scope **and** OpenFGA. Routes declare a permission (or `public=True`), and an architecture test enforces that. MCP tools and background jobs call the same helper.
- **Model versions are pinned per deploy** (`INKCAST_FGA_MODEL_ID`). A change is a new version, never an edit in place.
- **No business state in tuples.** Plan, quotas and approval status stay in the DB.

## Consequences
- OpenFGA (backed by Postgres) is a required backing service for the hosted SaaS. It is deployed by a Kustomize component, with images mirrored for air-gapped clusters.
- Spike F3.0.S1 measures the per-request latency of a check. Batching and `ListObjects` on workspaces keep list pages fast.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
