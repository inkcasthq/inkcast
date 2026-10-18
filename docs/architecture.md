# Inkcast architecture (v0.1)

## Components

```
                 ┌──────────────────────── Inkcast server ────────────────────────┐
 MCP client      │  OAuth 2.1 AS  ──►  Identity provider (pluggable)             │      Keycloak / any OIDC
 (Claude Code) ─►│  /register /authorize /token /revoke     oidc | github | password ├──►  GitHub OAuth app
                 │  consent page                                                  │
                 │                                                                │
                 │  MCP resource /mcp  ──►  tools  ──►  Publisher ──► Platform plug-ins ├──►  Medium API
                 │                                         │         (entry points)    ├──►  Forem/DEV API
 Browser ───────►│  /connect/{ticket} (enter platform keys)│                           │
                 │                                         ▼                           │
                 │                 Store (SQLite → Postgres)   ◄── Scheduler loop      │
                 └────────────────────────────────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `auth/provider.py` | OAuth 2.1 authorization server for MCP clients: dynamic client registration (RFC 7591), PKCE S256, single-use codes, rotating refresh tokens, revocation (RFC 7009). Metadata per RFC 8414 / RFC 9728 is served by the MCP SDK. |
| `auth/identity.py` | Who the person is. `OIDCIdentity` (Keycloak or any OpenID Connect provider, with PKCE), `GitHubIdentity`, `PasswordIdentity`. Each returns a stable identity key, `oidc:<issuer>#<sub>` or `github:<id>`. |
| `auth/pages.py` | Sign-in, consent and connect pages, with a strict Content-Security-Policy, no framing, and `no-store` caching. |
| `platforms/` | The `Platform` interface, built-ins (Medium, DEV) and the entry-point registry. |
| `story.py` | Platform-neutral story: front matter, per-platform overrides, image URL fixing, schedule validation. Pure: no I/O. |
| `publisher.py` | Fan-out to platforms, immediate sends, scheduled sends with retry (`60s`, `5m`, `30m`), cancel. |
| `storage.py` | Users, OAuth state, encrypted credentials, posts. |
| `service.py` | Use cases (platforms, connect, publish, posts, cancel), shared by the MCP tools and the REST API. |
| `models.py` | Request/response models. They are the single source for the tool schemas and the OpenAPI document. |
| `api.py` | REST API `/api/v1`, OpenAPI 3.1 at `/api/openapi.json`, Swagger UI at `/api` and ReDoc at `/api/redoc`, rendered from vendored, hash-locked bundles. |
| `observability.py` | JSON logs, Prometheus metrics. |
| `server.py` | MCP tools (thin renderings of the service), browser routes, probes, run modes (`all`/`web`/`worker`), coordinated shutdown. |

## Sign-in flow (what Claude Code does on `/mcp` → Authenticate)

1. `POST /mcp` without a token returns `401` with `WWW-Authenticate: Bearer resource_metadata=…`.
2. The client reads `/.well-known/oauth-protected-resource/mcp`, then `/.well-known/oauth-authorization-server`.
3. It registers itself at `POST /register` (dynamic registration).
4. It opens `/authorize` in the browser with PKCE. Inkcast stores a *pending* request and redirects to `/auth/login`.
5. The identity provider round-trip runs: Keycloak, GitHub or the password form, ending at `/auth/callback`.
6. The callback maps the identity to an Inkcast user (`upsert_user`) and shows the **consent page**, naming the client and its redirect host.
7. On approve, Inkcast issues a single-use code and redirects to the client. The client exchanges it at `/token` with the PKCE verifier.

## Security model

**Credentials and tokens**

- Platform credentials are encrypted with a Fernet key derived from `INKCAST_SECRET_KEY`. Rotating that key requires reconnecting platforms; a KMS-backed envelope is planned (issue 09).
- OAuth codes and access/refresh tokens are stored only as SHA-256 hashes.
- Access tokens last 1 hour and refresh tokens 30 days. A refresh revokes the whole token family.
- Platform keys never pass through the AI. `inkcast_connect` returns a one-time, 15-minute link instead. The key is verified against the platform before it is stored.

**Authorization**

- Consent is required for every authorization. Dynamically registered clients can't obtain tokens silently, which addresses the "confused deputy" risk in the MCP security guidance.
- Every tool resolves the user from the token's subject, and every query is scoped to that user.
- Hosted mode refuses to start unless sign-in is restricted (role, email domain, org or user list), or open sign-up is enabled explicitly.

**Transport and pages**

- DNS-rebinding protection checks `Host` headers on `/mcp`. `INKCAST_PUBLIC_URL` must be `https` unless it is localhost.
- Pages send a strict Content-Security-Policy, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.

## Cloud-native design

### Twelve-factor mapping

| Factor | How Inkcast does it |
|---|---|
| Config | Environment only. Secrets via `*_FILE`, so Kubernetes/OpenShift Secrets mount as files, never env vars. |
| Backing services | IdP, platform APIs and storage are attached resources, swappable by configuration (platform API URLs too). |
| Build, release, run | One image per commit, released by digest, configured per environment by Kustomize. |
| Processes | Stateless web tier (OAuth state, tokens and posts live in the store). SQLite in v0.1 pins one replica; Postgres lifts it. |
| Port binding | `:8787` app, `:9090` metrics + probes. |
| Concurrency | `inkcast web` scales horizontally; `inkcast worker` runs the scheduler. Claiming is atomic, so several workers are safe. |
| Disposability | SIGTERM stops intake, lets in-flight requests finish (25 s grace) and **finishes the current scheduler tick**, so no post is half-sent. |
| Logs | JSON to stdout, no files. OAuth codes never logged (HTTP client and access logs held at WARNING). |
| Admin processes | `inkcast` CLI modes; key rotation and migrations come later as one-off Jobs. |

### Runtime contract

| Probe | Path (metrics port) | Meaning |
|---|---|---|
| startup / liveness | `/livez` | Process is serving |
| readiness | `/readyz` | Storage answers; the scheduler (if this process runs it) ticked recently |
| metrics | `/metrics` | Prometheus text format, on a port that is never routed publicly |

**Metrics:**

- `inkcast_build_info`
- `inkcast_publish_total{platform,status}`
- `inkcast_scheduler_ticks_total`
- `inkcast_posts{status}`
- `inkcast_posts_due` (queue depth, ready for a KEDA scaler)

**Pod security:**

- the container runs as an arbitrary non-root UID in group 0 (OpenShift `restricted-v2`, Kubernetes PSS `restricted`);
- read-only root filesystem; all capabilities dropped; seccomp `RuntimeDefault`;
- no service-account token.

**Duplicate-safe publishing:**

- Connection errors and 429s are retried.
- A timeout *after* a publish request was sent, or a 5xx on it, marks the post `unknown` for a human to check. Retrying could post twice.

### CNCF building blocks

Maturity as listed on cncf.io/projects (checked 2026-10-18); re-check before relying on it.

| Concern | Choice | CNCF status | Where |
|---|---|---|---|
| Orchestration | Kubernetes / OpenShift | Graduated | `deploy/kustomize` (overlays: openshift, kubernetes) |
| Packaging | Kustomize now; Helm chart planned | Helm: Graduated | issue 26 |
| GitOps | Argo CD or Flux | Graduated | `deploy/kustomize/environments`, issue 29 |
| Identity | Keycloak | Incubating | sign-in, admin portal |
| Ingress | OpenShift Route / Gateway API HTTPRoute | Gateway API is a Kubernetes SIG project | overlays |
| TLS | cert-manager | Graduated | Gateway certificates |
| Secrets | External Secrets Operator | Sandbox (2022) | `components/external-secrets` |
| Policy | Kyverno (image signature + SBOM verification) | Graduated | `deploy/policies` |
| Network | NetworkPolicy default-deny; Cilium FQDN egress or OVN EgressFirewall | Cilium: Graduated | base + `components/egress-*` |
| Metrics | Prometheus (ServiceMonitor) | Graduated | `components/servicemonitor` |
| Traces | OpenTelemetry | Graduated | issue 27 |
| Logs | stdout JSON → Fluent Bit / Fluentd | Fluentd: Graduated | platform logging |
| Autoscaling | KEDA on `inkcast_posts_due` / media queue | Graduated | social epic, issue 17 |
| Database | PostgreSQL via CloudNativePG | Sandbox (2025) | issue 10 |
| Object storage | Rook-Ceph RGW (on-prem) or cloud S3 | Rook: Graduated | social epic, issue 17 |
| Events | CloudEvents for webhooks in and out | Graduated | social epic, issue 19 |
| Supply chain | in-toto/SLSA provenance, Sigstore cosign, SPDX SBOM | in-toto: Graduated (Sigstore is OpenSSF) | `.github/workflows/publish.yml` |
| Runtime security | Falco | Graduated | optional, cluster-level |

### Deployment topologies

| Stage | Topology |
|---|---|
| v0.1 | One pod (`inkcast all`), SQLite on an RWO PVC, `Recreate` strategy |
| Phase 1 | `inkcast web` Deployment (≥ 2 replicas, RollingUpdate, PodDisruptionBudget, topology spread) + `inkcast worker` Deployment + CloudNativePG cluster |
| Phase 2 | Media workers scaled by KEDA, object storage, CloudEvents webhooks (social epic) |

## Known limits of v0.1

- **Storage:** single node, SQLite.
- **Scheduler:** with SQLite, run `inkcast all` as a single pod. With Postgres (issue 10), `web` and `worker` split.
- **Platforms:** Medium cannot edit or delete via its API, and DEV unpublishing needs the platform UI. Inkcast cancels only what hasn't been sent.
- **Images:** they must be public URLs. Uploading to Medium's image endpoint is planned.
