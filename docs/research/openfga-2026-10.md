# Inkcast: OpenFGA behaviour that matters for our model and engines

*Research date: 2026-10-22, for issues #72 (engine parity) and #71 / PR #99 (roles scoped to their organization). Sources:*
- *the pinned code, read over git: the `fga` CLI v0.8.2 (`.github/workflows/ci.yml`, `FGA_VERSION`), whose `go.mod` embeds `github.com/openfga/openfga v1.22.0`;*
- *OpenFGA v1.22.0's `pkg/server/config/config.go`;*
- *the official docs and blog, read on that date with a web-fetch tool.*

*Anything not confirmed in source or on an official page is flagged **[UNVERIFIED]**. Disagreements between sources are flagged **[CONFLICT]**.*

---

## TL;DR

| Question | Answer (OpenFGA v1.22.0, pinned) | What Inkcast does |
|---|---|---|
| Is a userset a member of itself, e.g. `check(organization:acme#member, member, organization:acme)`? | **Yes, today.** The default check engine answers true from the model alone (`IsSelfDefining`). | The local engine matches it since PR #108, with a yaml case pinning it. |
| Will that stay? | **No.** The new "weighted graph" check engine answers **false** unless stored tuples support it. It is experimental in v1.22.0 (`weighted_graph_check`), and no changeover date is set. | The yaml case is an early warning; flip the local engine when the pinned version flips. |
| Resolution depth | `DefaultResolveNodeLimit = 25`, `DefaultResolveNodeBreadthLimit = 10`. | `MAX_DEPTH = 25`. The model peaks at about 10 levels. |
| BatchCheck | `DefaultMaxChecksPerBatchCheck = 50`; concurrency 50. | `MAX_BATCH_CHECK = 50`. `OpenFgaEngine` splits larger batches into chunks; the local engine refuses (#72). |
| Writes | `DefaultMaxTuplesPerWrite = 100`. | `MAX_TUPLES_PER_WRITE = 100`. |
| ListObjects / ListUsers | 1000 results, 3 s deadline each. | `MAX_LIST_OBJECTS = 1000`. Lists use ListObjects on workspaces, then filter in the database (ADR-0007). |
| Errors inside `or` / `and` | Today's engine can stop on an error in one union branch. The new engine tolerates errors in union branches and fails fast on intersection and exclusion. | The local engine raises on the first error; it fails closed (#72). |
| Model compatibility with the new engine | It refuses recursion through `and` or `but not`, a `rel from parent` whose `rel` is missing on an allowed type, and alias traversal in Check. | **Our model is compatible:** no recursion at all, no `but not`, and every `from` resolves (checked 2026-10-22 against #99's model). |

---

## 1. Usersets that are members of themselves

- **Today (v1 engine, the default in v1.22.0):** `check(document:A#viewer, viewer, document:A)` is true, because the relation exists in the model, not because of any tuple. OpenFGA's Go package `v2breaking` names this shape `self_referential_userset`: "v1 always returned TRUE … v2 evaluates against the schema and returns FALSE".
- **Where we met it:** PR #99 added `member_assignee: assignee and member from org` to `role`. When an organization assigns its own role to `organization:acme#member`, the `member from org` step asks whether `organization:acme#member` is a `member` of `organization:acme`. The v1 engine says yes; our local engine said no until PR #108.
- **Effect on enforcement: none.** `require()` only checks `user:<id>`. The difference shows only when a userset is checked as the subject (the model tests, and Expand-style tooling).
- **The weighted-graph engine reverses it:** "A relation existing in the model is not sufficient evidence that a group has access to an object." The same post says alias traversal in Check also stops: a tuple stored for `doc#allowed` no longer answers for an alias relation.
- **Our position:** the local engine follows the **pinned** OpenFGA version, because it stands in for the server in tests and small self-hosts. When the pin moves to a version where the weighted graph is the default, the yaml case added by #108 fails in CI's `fga model test`. At that point we flip the local engine in the same PR.

## 2. The new check engine (weighted graph)

- **Status:** ListObjects already uses it. Check is next. Models it can't handle fall back to the legacy algorithm, a fallback the post says "will be removed soon". No version and no date are set ("A date for the final changeover has not been set at this time", blog post of 21 July 2026).
- **Flags in v1.22.0** (`pkg/server/config/config.go`): `weighted_graph_check`, `shadow_weighted_graph_check`, `shadow_check`, `datastore_throttling`, `enable-check-optimizations`, and others. **[UNVERIFIED]** the exact behaviour of `shadow_weighted_graph_check`. By its name and the `shadow_check` pattern, it runs both engines and logs differences.
- **What it refuses when a model is built:**
  - recursion through `and` or `but not` (pure union recursion is fine);
  - `rel from parent` where an allowed type of `parent` lacks `rel`.

  It also errors at request time on a userset or wildcard subject that reaches a `but not`.
- **Depth:** the fixed limit of 25 is replaced by throttling, graph flattening and cancellation. Our `MAX_DEPTH = 25` stays right for the v1 engine we pin.
- **Error handling:** union branches tolerate errors; intersection and exclusion fail fast.

## 3. Custom roles scoped to an organization

- OpenFGA's own custom-roles guide models a role as its own type with an `assignee` relation. **It doesn't cover tenant isolation:** nothing scopes a role to the organization that owns it. The `org` relation on `role`, and the guard `assignee and member from org` (PR #99), are our addition.
- **What the model can close, and what it can't:**
  - **Case 1, closed by the model:** a foreign role assigned to this organization's members grants nothing.
  - **Case 2, can't be closed in the model:** a person who is a member of both organizations. OpenFGA can't compare two organizations, so the grant API (E3.F3.8.S2) and the reconciler (E3.F3.8.S3) close it.
  - The model also closes case 1 only while a role has exactly one `org` tuple. OpenFGA can't enforce "exactly one", so a role's `org` is written once, never added to, and a second one is drift (#72).

## 4. Limits, side by side

| Limit | OpenFGA v1.22.0 default | Inkcast constant |
|---|---|---|
| Resolution depth | 25 (`DefaultResolveNodeLimit`) | `MAX_DEPTH = 25` (`libs/authz/engine.py`) |
| Resolution breadth | 10 (`DefaultResolveNodeBreadthLimit`) | none (the local engine is sequential) |
| Checks per BatchCheck | 50 | `MAX_BATCH_CHECK = 50` |
| Tuples per Write | 100 | `MAX_TUPLES_PER_WRITE = 100` (`libs/authz/openfga.py`) |
| ListObjects results / deadline | 1000 / 3 s | `MAX_LIST_OBJECTS = 1000` |
| ListUsers results / deadline | 1000 / 3 s | not used yet |
| Types per model / model size | 100 / 256 KiB | well under |
| Condition evaluation cost | 100 | no conditions yet |

These are OpenFGA **server** settings. If a deployment changes them (`OPENFGA_MAX_CHECKS_PER_BATCH_CHECK` and the like), our constants must follow. That's tracked as part of the engine-parity work in #72.

## 5. Actions

1. **Keep the pin and the local engine in step.** When `FGA_VERSION` (CLI) or the OpenFGA server image is bumped, read the release notes for check-semantics changes, and run the yaml suite in both engines (already in CI).
2. **When the OpenFGA component ships (E3.F3.9):** run staging with `shadow_weighted_graph_check` (**[UNVERIFIED]** that it only logs) to collect differences before the new engine becomes the default.
3. **Model rules for new relations** (add to the contributor checklist with the next model change):
   - no recursion through `and` or `but not`;
   - every `rel from parent` must exist on every type `parent` allows;
   - never rely on a group being a member of itself.

## Sources

- [OpenFGA's Move to Weighted Graph Resolution: What's Changing](https://openfga.dev/blog/weighted-graph-upcoming-changes), 21 July 2026.
- [`v2breaking` package (Go docs)](https://pkg.go.dev/github.com/openfga/openfga/pkg/server/commands/v2breaking): the request shapes whose v1 and v2 answers differ.
- [Configuring OpenFGA](https://openfga.dev/docs/getting-started/setup-openfga/configure-openfga): experimental flags.
- [Modeling custom roles](https://openfga.dev/docs/modeling/custom-roles).
- [Usersets](https://openfga.dev/docs/modeling/building-blocks/usersets).
- OpenFGA v1.22.0 source (`pkg/server/config/config.go`) and openfga/cli v0.8.2 `go.mod`, read over git on 2026-10-22.
- **[CONFLICT]** The GitHub releases page fetched on the same day listed v1.19.0 as the latest release. The CLI's `go.mod` and the v1.22.0 tag (6 October 2026) show it was stale, so the version facts here come from the tags.

---
<!-- inkcast-nav -->
[Docs](../../docs/README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
