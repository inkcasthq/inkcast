# Inkcast

**Write once in markdown. Publish everywhere — from your AI assistant.**

**[Docs](docs/README.md)** · [Start here: bootstrap](docs/bootstrap.md) · [API reference](docs/README.md#engineering) · [Licensing](LICENSING.md) · [Commercial licences](legal/COMMERCIAL-LICENSE.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

Inkcast is an MCP server that lets Claude Code, or any MCP client, publish, cross-post and schedule your writing on Medium, DEV and more. You manage it all from the terminal, with a canonical link back to your own blog on every copy.

```text
> Publish docs/blog/why-we-built-an-agent.md to Medium and DEV with the subtitle
  "Lessons from year one" and the canonical URL pointing to our blog

● inkcast_publish (dry run)  → per-platform preview checked
● inkcast_publish            → “Why we built an agent” — Lessons from year one
  - medium: published → https://medium.com/@ada/why-we-built-an-agent-…
  - devto: published → https://dev.to/ada/why-we-built-an-agent-…
```

See it in use: [screenshots](docs/screenshots.md), from adding Inkcast to an AI assistant to deleting an account.

## Why it's built this way

| Principle | What it means in practice |
|---|---|
| **Plug-in platforms** | Each platform is a class behind one small interface. Medium and DEV ship built in; others install as Python packages through the `inkcast.platforms` entry point. See [docs/writing-a-platform-plugin.md](docs/writing-a-platform-plugin.md). |
| **Pluggable sign-in** | OpenID Connect (Keycloak recommended), a GitHub OAuth app, or a single password for local use. With Keycloak, Google/GitHub/Microsoft social login and account linking happen in Keycloak, not in Inkcast. |
| **Secrets never touch the AI** | Platform keys are entered on a one-time browser page, never pasted into chat. They are encrypted at rest. OAuth codes and tokens are stored only as SHA-256 hashes. |
| **Explicit consent** | Every MCP client that registers itself is shown a consent page before it gets a token. Codes are single-use; refresh tokens rotate. |
| **Platform-correct output** | Each plug-in knows its platform's rules. Medium needs the title and subtitle inside the body and uses only 3 tags. DEV wants lowercase alphanumeric tags and no title in the body. Writers in a Medium publication can only create drafts. |
| **Own scheduler** | Scheduled posts live in Inkcast's database and go out on time. Rate limits and server errors are retried automatically. |
| **Git-native** | Push markdown to a repository and the [GitHub Action](docs/git-publishing.md) publishes new stories and updates changed ones in place. It never posts the same file twice. |
| **One codebase, many services** | One image runs any service, selected by `INKCAST_APP_NAME` (`IdentityService`, `PublishingApi`, `ConnectorService`, `SchedulerWorker`, `Gateway`), or all of them at once. Shared libraries and service boundaries are enforced by tests. See [architecture](docs/architecture.md). |

More detail on the design is in [docs/architecture.md](docs/architecture.md).

## Platforms

| Platform | Status | Credentials | Notes |
|---|---|---|---|
| Medium | ✅ built in | Integration token | Medium stopped issuing new tokens on 1 Jan 2025; tokens created earlier still work. Without one, Inkcast gives you Medium's *Import a story* steps instead. That import also sets the canonical link. |
| DEV (dev.to) | ✅ built in | API key | Works with any Forem community via `INKCAST_API_URL_DEVTO`. |
| YouTube, Facebook Pages | 🧭 designed | OAuth per platform | Video upload, audience and AI-disclosure fields, platform review gates. See [video platforms and batch review](docs/design/video-platforms-and-review.md). |
| X, Instagram, TikTok | 🧭 designed | OAuth per platform | Needs per-platform OAuth, a media pipeline, async publishing and platform review gates. See [the social epic](docs/design/social-platforms-epic.md). |
| Hashnode, WordPress, Ghost, … | 📋 planned | | See [docs/issues/](docs/issues/). |

## Tools

| Tool | What it does |
|---|---|
| `inkcast_platforms` | Lists the platforms and which ones you've connected. |
| `inkcast_connect` | Returns a one-time link where you enter a platform's credentials. |
| `inkcast_disconnect` | Deletes your stored credentials for a platform. |
| `inkcast_destinations` | Lists Medium publications, or finds DEV organizations. |
| `inkcast_publish` | Publishes now, schedules, or creates drafts on one or more platforms. Supports `dry_run`. |
| `inkcast_posts` | Lists your scheduled, published, draft, failed, unconfirmed and cancelled posts. |
| `inkcast_cancel` | Cancels a scheduled, recovering or unconfirmed post. A recovering or unconfirmed one (the platform never said whether it arrived) may already be on the platform; the reply says so. |
| `inkcast_resend` | Sends an unconfirmed post again, now, as one new post, on your decision; the old one is cancelled. Inkcast never sends an unconfirmed post again on its own. |
| `inkcast_sync` | Syncs files from a repository: publishes new ones, updates changed ones in place, skips the rest. |
| `inkcast_create_token` | Gives you a link to create a personal access token for CI. You see the token; the AI never does. |
| `inkcast_delete_account` | Gives you a one-time link to delete your account. You sign in again, read what goes and what stays, and confirm at the sign-in provider (Keycloak); Inkcast then erases your data. The AI deletes nothing. |

## Publish from Git

```yaml
# .github/workflows/publish.yml in your blog repository
- uses: inkcasthq/inkcast/action@<commit-sha>
  with:
    url: ${{ vars.INKCAST_URL }}
    token: ${{ secrets.INKCAST_TOKEN }}   # personal access token, scope stories:sync
    paths: "posts/**/*.md"
```

On pull requests it runs a dry run and can comment the preview. On `main` it publishes. The full guide, the CLI for other CI systems (`inkcast sync`), and what each result means are in [docs/git-publishing.md](docs/git-publishing.md).

## REST API and docs

Every tool is also a REST endpoint under `/api/v1`. Authenticate with a Bearer token: an OAuth access token (scope `inkcast`), or a personal access token with narrower scopes.

| URL | What |
|---|---|
| `/api` (or `/api/docs`) | Swagger UI |
| `/api/redoc` | ReDoc reference |
| `/api/openapi.json` | OpenAPI 3.1 document |

Both doc pages render from **vendored, hash-locked bundles** in `src/inkcast/static/vendor`:

- **Pinned versions:** Swagger UI 5.32.11 and ReDoc 2.5.3, pinned in `ASSETS.lock`.
- **No CDN:** nothing is fetched at run or build time, so the pages work on air-gapped clusters.
- **Strict CSP:** the pages carry a strict Content-Security-Policy with no inline scripts.
- **Long-lived caching:** assets are served with an ETag and a one-year `immutable` cache under a content-hash URL.

`scripts/vendor-assets.sh` verifies the bundles offline and upgrades them from npm, checking npm's published integrity digest, never a CDN.

## Quick start (local, single user)

```bash
python3 -m venv .venv    # Python ≥ 3.12 (the image runs 3.14); everything below runs from it
.venv/bin/pip install .
export INKCAST_LOGIN=password INKCAST_PASSWORD='a long passphrase here' \
       INKCAST_SECRET_KEY=$(.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(48))')
.venv/bin/inkcast        # serves http://localhost:8787/mcp
```

Then set it up in Claude Code:

1. Add the server once:

   ```bash
   claude mcp add --transport http inkcast http://localhost:8787/mcp
   ```

2. Run `/mcp`, select **inkcast**, choose **Authenticate**, sign in and approve.
3. Ask *"connect DEV to Inkcast"*, open the link Claude gives you, and paste your DEV API key.
4. Ask *"publish posts/hello.md to DEV"*.

Claude reads the file and passes its text to Inkcast. Inkcast never touches your filesystem, so it works the same locally or hosted.

## Hosted (team or SaaS) with Keycloak

### 1. Set up Keycloak

1. Create a realm, e.g. `inkcast`.
2. **Clients → Create client**, OpenID Connect, client ID `inkcast`:
   - Client authentication **on**, Standard flow **on**.
   - Valid redirect URI: `https://inkcast.example.com/auth/callback`.
   - Under *Advanced*, set PKCE method to **S256**.
   - Copy the client secret.
3. **Realm roles → Create** `inkcast-user`. Assign it to the people who may use Inkcast. For open SaaS sign-up, add it to the realm's **default roles**.
   For **Delete my account** (`inkcast_delete_account`, `POST /api/v1/account/deletion-link`): enable the `delete_account` required action, add the `account` client's `delete-account` role to the default roles, record the `DELETE_ACCOUNT` event (Realm settings → Events), set the client's *Home URL* (`baseUrl`) to `https://inkcast.example.com/account/return` so Keycloak's info pages (the "deleted" page among them) link back, and run the user sync (`INKCAST_KEYCLOAK_SYNC_CLIENT_ID`, below): Keycloak deletes the account on the person's own authority and Inkcast erases their data once the sync's read-only service account confirms the account is gone. Deleting a user in the Keycloak admin console is a deletion too: Inkcast cuts the person off at once and erases their data `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` later (30 by default), so a mistaken deletion restored from a backup under the same id meanwhile keeps everything. Disabling a user only cuts them off. `deploy/keycloak/realm-inkcast.json` has all of it.
4. Optional social login: **Identity providers → Google / GitHub / Microsoft…**. Keycloak's *first broker login* flow links a social account to an existing user with the same verified email. Inkcast always sees one stable user ID per person.

### 2. Run Inkcast on OpenShift or Kubernetes

Permission checks run in OpenFGA ([design](docs/authorization.md)), on a PostgreSQL that the [CloudNativePG](https://cloudnative-pg.io) operator runs: install the operator first (its release manifest, server-side; the version CI tests is in `scripts/versions.env`).

```bash
oc new-project inkcast
oc create secret generic inkcast-secrets \
  --from-literal=secret-key="$(openssl rand -base64 48)" --from-literal=oidc-client-secret=… \
  --from-literal=fga-api-token="$(openssl rand -base64 48)"    # OpenFGA's preshared key; Inkcast reads the same entry
# edit deploy/kustomize/base/kustomization.yaml (public URL, OIDC issuer, image) and the Route host, then:
oc apply -k deploy/kustomize/overlays/openshift          # or: kubectl apply -k deploy/kustomize/overlays/kubernetes
oc -n inkcast rollout status deploy/inkcast --timeout=15m   # Ready on its own: no id to write or pin by hand
```

The store and the model are written by the pods themselves: the init container `authz-model` of Inkcast's pod runs `inkcast authz write-model` with the pod's own image before the server starts, and the server then pins the store `INKCAST_FGA_STORE_NAME` names and its version equal to the packaged model. Every pod start runs it, so a release, a rollback or a restart writes what its own image packages, and a run that finds the version writes nothing; pods starting together converge on one store. On a fresh install the init container fails and is restarted by the kubelet until the migrate Job has run and OpenFGA serves (`oc -n inkcast logs deploy/inkcast -c authz-model` says why), so the pod is Ready up to five minutes after that (the kubelet's back-off): the Deployment's progress deadline is 15 minutes, as is the wait above. A model change travels with the image; a rollback is the image's revert. To hold one version by hand instead, merge `INKCAST_FGA_STORE_ID` and `INKCAST_FGA_MODEL_ID` into `inkcast-config`: both set, they win, nothing is resolved and the init container writes nothing. To keep the in-process engine instead (a small self-host), leave `components/openfga` out of your overlay and set `INKCAST_AUTHZ_ENGINE=local` with `INKCAST_AUTHZ_ALLOW_LOCAL=true`.

**The lab: staging and prod side by side (OpenShift Local).** Both environments run on one CloudNativePG cluster in its own namespace, `inkcast-db` (`deploy/kustomize/shared/inkcast-db`: one Postgres pod for both, a staging and a prod database each owned by its own login role, a NetworkPolicy that admits 5432 from OpenFGA's pods in `inkcast-staging` and `inkcast-prod` and nothing else), so the lab's memory holds both. `environments/staging` and `environments/production` set those namespaces and drop the component's per-namespace `Cluster`; a self-host using the overlays alone keeps it. In order:

```bash
oc apply -k deploy/kustomize/shared/env-secrets-admission       # first: each environment's env-secrets creates only its own Secrets
oc apply -k deploy/kustomize/shared/inkcast-db                   # the namespace, the Cluster, both databases, the policy, Roles
oc apply -k deploy/kustomize/shared/keycloak-access              # each environment's Role for its realm's Secret in keycloak
oc -n inkcast-db wait --for=condition=Ready cluster/inkcast-pg --timeout=10m
oc new-project inkcast-staging && oc new-project inkcast-prod
scripts/deploy/ghcr-pull-secret.sh staging <github-username> <token-file>   # the private image: see below
scripts/deploy/ghcr-pull-secret.sh prod <github-username> <token-file>
oc apply -k deploy/kustomize/environments/staging                # then environments/production the same way
oc -n inkcast-staging wait --for=condition=complete job/openfga-migrate --timeout=10m
oc -n inkcast-db get database                                    # APPLIED true for both, once their roles have their Secrets
oc apply -k deploy/kustomize/shared/keycloak-realms              # Mailpit, and both realms imported once (below), in any order:
oc -n keycloak get keycloakrealmimports                          # each import waits for its realm Secret, then runs; Done for both
oc -n inkcast-staging rollout status deploy/inkcast --timeout=15m   # the authz-model init container, then Ready
```

`oc delete -k deploy/kustomize/shared/inkcast-db` deletes the Cluster and, with it, its volume: both environments' authorization data. `databaseReclaimPolicy: retain` keeps a database when its `Database` object is deleted, not when the Cluster is.

**The realms as code.** Each environment signs people in against its own realm on the lab's Keycloak (namespace `keycloak`, CR `keycloak`, run by the operator): `inkcast-staging` and `inkcast-prod`, each a `KeycloakRealmImport` in `deploy/kustomize/shared/keycloak-realms` that `scripts/gen-realm-imports.py` renders from the one realm file, `deploy/keycloak/realm-inkcast.json`, unchanged but for its name, the environment's public URL and the mail; `--check` fails the tests when the realm file, an environment or the Mailpit Service moved without regenerating. The import Job reads the client secrets from `Secret inkcast-<side>-realm` through `spec.placeholders`, so nothing secret is in Git. The order does not matter: each placeholder is an environment entry of the Job's pod from that Secret (`optional: false`), so an import applied before env-secrets made the Secret keeps its pod waiting in `CreateContainerConfigError`, the kubelet retries it, and the import completes on its own once the Secret exists, without any restart of the Keycloak server (measured on OpenShift Local, 2026-10-29: Done 40 s after the Secret; [docs/research/realm-import-before-secret-2026-10.md](docs/research/realm-import-before-secret-2026-10.md)). The values that must agree, per side (`tests/test_realm_imports.py` checks each row):

| Value | Set in | Must agree with |
|---|---|---|
| `INKCAST_PUBLIC_URL=https://inkcast-<side>.apps-crc.testing` | `environments/<env>` | the Route's host in the same file; the client's `redirectUris` (`…/auth/callback`), `webOrigins` and `baseUrl` in the generated import |
| `OIDC_ISSUER=https://keycloak.apps-crc.testing/realms/inkcast-<side>` | `environments/<env>` | the import's `metadata.name` and `spec.realm.realm`, `inkcast-<side>`; the generator refuses any other realm |
| `OIDC_CLIENT_ID=inkcast-identity`, `INKCAST_KEYCLOAK_SYNC_CLIENT_ID=inkcast-sync` | `environments/<env>` | the realm file's two clients: sign-in, and the read-only service account of the user sync, on in both environments |
| `OIDC_CLIENT_SECRET_FILE` → `inkcast-secrets/oidc-client-secret`; `INKCAST_KEYCLOAK_SYNC_SECRET_FILE` → `inkcast-secrets/keycloak-sync-secret` | the base Deployment; `environments/<env>` | `inkcast-<side>-realm/identity-client-secret` and `…/sync-client-secret` in `keycloak`, which the import feeds into `${KC_INKCAST_CLIENT_SECRET}` and `${KC_INKCAST_SYNC_SECRET}`: one value each per side, written to both Secrets by `env-secrets` (#323, below) |
| `smtpServer`: `mailpit.keycloak.svc:1025`, no auth | the generated import | the Mailpit Service beside it; only Keycloak's pods may reach it |

An import creates its realm once and never changes it (`kc.sh import --override=false`; the operator's guide: "The Realm Import CR only supports creation of new realms"). A re-apply with the realm in place changes nothing. Change a realm that has people through the admin console or API, and the realm file in the same pull request. Deleting a realm deletes every account in it, and the user sync then treats each person as deleted (cut off, then erased after `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS`; someone who signs up again is a new account), so delete the realm and its `KeycloakRealmImport` and apply again only for a realm with no accounts to keep. The verification and password-reset mail of both realms lands in Mailpit and nowhere else: `oc -n keycloak port-forward svc/mailpit 8025:8025`, then `http://localhost:8025`.

**No Secret to make: the cluster generates them.** The init container `env-secrets` of OpenFGA's pod (`deploy/kustomize/components/env-secrets`, running `inkcast env-secrets` from the release's image) makes each environment's Secrets at every start of that pod, before OpenFGA reads them: `inkcast-<side>/inkcast-secrets` (Inkcast's `secret-key`, OpenFGA's preshared key `fga-api-token`, and the Keycloak clients' `oidc-client-secret` and `keycloak-sync-secret`), `inkcast-<side>/openfga-db-app` (the database URI), and the other side of each pair: `inkcast-db/inkcast-pg-openfga-<side>`, the role's password that CloudNativePG applies, and `keycloak/inkcast-<side>-realm`, the client secrets the realm import reads (`identity-client-secret`, `sync-client-secret`). An absent value is generated (Python's `secrets`); a value on one side of a pair is copied to the other, so a namespace deleted and applied again gets its values back; a value is never replaced; two sides that disagree stop the new pod, naming the pair, while the previous pod keeps serving, and the kubelet retries it until the wrong key is removed. Its output names Secrets and keys, never a value. Its ServiceAccount `inkcast-env-secrets` reads and patches only its own Secrets, by name, in `inkcast-<side>`, `inkcast-db` and `keycloak` (the Roles in `components/env-secrets`, `shared/inkcast-db` and `shared/keycloak-access`; until the shared ones are applied, the pod stops naming them). Kubernetes RBAC cannot narrow `create` to a name, so the admission policy in `shared/env-secrets-admission`, applied first, does: each side's account creates only its own four Secrets, so staging's run cannot choose production's values.

To generate a value again, remove it from both sides of its pair, then `oc -n inkcast-<side> rollout restart deploy/openfga`. The database password (delete both `inkcast-db/inkcast-pg-openfga-<side>` and `inkcast-<side>/openfga-db-app`) changes in PostgreSQL as soon as it is written (the Secret's `cnpg.io/reload` label), and the new OpenFGA pod connects with it; PostgreSQL checks a password when a connection opens, so the previous pod keeps the connections it has and fails to open new ones until it is replaced. A Keycloak client secret (the key in both `keycloak/inkcast-<side>-realm` and `inkcast-secrets`) changes in Keycloak only when the realm is imported again, and Inkcast's sign-in fails until then. `secret-key` seals the platform credentials stored at rest: a new one makes every sealed credential unreadable, so generate it again only for an environment with none. Credentials are passwords (cert-manager client certificates instead: #299). The URI OpenFGA reads is `postgres://openfga_<side>:…@inkcast-pg-rw.inkcast-db.svc:5432/openfga_<side>?sslmode=require`: always TLS, with the server's certificate unverified until the cluster's CA travels with #299; the server admits each role over TLS to its own database and rejects it anywhere else (`pg_hba` in `cluster.yaml`), so a staging role cannot open `openfga_prod`.

**The image pull Secret.** `ghcr.io/inkcasthq/inkcast` is a private package, so each app namespace needs the Secret `ghcr-pull` (`kubernetes.io/dockerconfigjson`, for ghcr.io) until it is public. Make a classic personal access token with `read:packages` only (not the command-line tool's own token, which carries far more), save it to a file, and run the script: the token goes from the file to `oc` on stdin, never in an argument or on the screen; a run with a new file rotates it. The environments' manifests name the Secret both ways, and either alone pulls: on the ServiceAccounts of every pod running the image, `inkcast` and `inkcast-env-secrets` (OpenFGA's pod, whose init container runs it; the field `oc secrets link --for=pull` would write; it also covers a pod added later, such as the restore Job of `scripts/ops/restore.sh`), and on each workload's pod spec (the Deployment, the backup CronJob and OpenFGA's Deployment), which covers a pod whose ServiceAccount is not wired. The overlays carry neither: a self-host pulls its own image. When the package goes public: remove the three `ghcr-pull` patches from `deploy/kustomize/environments/staging` and `production` and the two `ghcr-pull` entries of `deploy/kustomize/components/env-secrets` in a pull request, apply both environments, then `oc -n inkcast-staging delete secret ghcr-pull` and the same in `inkcast-prod`. A Secret that is still named but missing only makes the kubelet warn, so the order is not critical.

**What the manifests set up:**

- **Pod security:** restricted, i.e. arbitrary non-root UID, read-only root filesystem, no capabilities. OpenFGA and its database run the same way.
- **Network:** default-deny NetworkPolicy, plus optional FQDN egress allow-lists (Cilium, or OVN EgressFirewall).
- **Probes and metrics:** `/livez` and `/readyz` probes, with Prometheus metrics on a separate, unrouted port.
- **Exposure:** an edge-TLS Route with HSTS on OpenShift, or a Gateway API HTTPRoute on Kubernetes.
- **Outbound trust on OpenShift:** Inkcast trusts the cluster's CA bundle, so a Keycloak whose certificate comes from your own CA verifies. The bundle is the ConfigMap `inkcast-trusted-ca`, which the Cluster Network Operator fills (the system CAs plus `proxy/cluster.spec.trustedCA`); `SSL_CERT_FILE` names it.

Optional components add a ServiceMonitor, an ExternalSecret and a Kyverno image-signature policy. The cloud-native design is in [docs/architecture.md](docs/architecture.md); the build and deploy pipeline is in [docs/cicd.md](docs/cicd.md).

**Without Kubernetes:**

```bash
docker run -d --env-file .env -v inkcast-data:/data -p 8787:8787 ghcr.io/inkcasthq/inkcast:0.1.0
```

Put it behind HTTPS. Then everyone adds it with:

```bash
claude mcp add --transport http inkcast https://inkcast.example.com/mcp
```

No Keycloak yet? Set `INKCAST_LOGIN=github` with a GitHub OAuth app. Its callback URL is `https://inkcast.example.com/auth/callback`. Restrict access with `GITHUB_ALLOWED_ORGS` or `GITHUB_ALLOWED_USERS`.

## Writing a story

Front matter is optional. Arguments in your request override it, and a per-platform block overrides both for that platform:

```yaml
---
title: Why we built an agent
subtitle: Lessons from year one              # Medium: shown under the title · DEV: description
tags: [ai, agents, developer-tools]          # each platform applies its own limits, with warnings
canonical_url: https://blog.example.com/why-we-built-an-agent
cover_image: https://blog.example.com/img/cover.png
platforms: [medium, devto]
publish_at: 2026-10-21T09:00:00-05:00        # omit to publish now; a UTC offset is required; up to 730 days ahead
image_base_url: https://blog.example.com/why-we-built-an-agent/   # fixes relative image paths
medium:
  title: A punchier title for Medium
  destination: Better Programming            # publication by name, slug or id
  publish_status: unlisted                   # public | draft | unlisted
devto:
  destination: my-org                        # organization username or id
  series: Building agents
---

# Why we built an agent        ← used as the title if none is given; never duplicated

Your story…
```

Some behaviour to know:

- **Partial failure:** a failure on one platform doesn't stop the others. The reply lists the result per platform.
- **Preview first:** `dry_run` shows exactly what each platform will receive.

## Configuration

All settings are environment variables; see [.env.example](.env.example). Each service checks only the settings it needs, and refuses to start with exit code 78 and a list of what to fix.

| Variable | Default | |
|---|---|---|
| `INKCAST_APP_NAME` | `All` | which service(s) this process runs, CamelCase and comma-separated (`inkcast services` lists them) |
| `INKCAST_PATH_PREFIX` | `/` | `/`, `auto` (each service under `/<full-name>`, e.g. `/publishing-api`), or a path |
| `INKCAST_UPSTREAM_<SERVICE>` | | Gateway only: e.g. `INKCAST_UPSTREAM_PUBLISHING_API=http://inkcast-publishing-api:8787` |
| `INKCAST_SCHEDULER_INTERVAL` | `20` | seconds between scheduler ticks (each tick claims and sends the posts due), `1` to `60`: `0` is a tight loop, and 120 s or more fails the on-time KPI by design |
| `INKCAST_OVERDUE_GRACE` | `600` | seconds past due before a scheduled post counts as overdue (metric and the critical `InkcastPostsOverdue` alert); at least twice `INKCAST_SCHEDULER_INTERVAL`, or posts due between two ticks count as overdue in normal operation, and at most `3600`: beyond an hour the alert, the only signal that the scheduler is behind or stopped, stays quiet that long |
| `INKCAST_PUBLISH_MAX_ATTEMPTS` | `4` | sends in all for a scheduled post whose send certainly never happened (a connection failure, a 429) before it is marked `failed`; `1` to `10`. A send that may have gone out (a timeout after sending, a 5xx) is never retried: the post is `unconfirmed` for a person to check |
| `INKCAST_PUBLISH_RETRY_DELAYS` | `60,300,1800` | seconds between those sends, comma-separated: 1 to 9 whole numbers, each `30` to `21600`, never decreasing; the last one repeats. The default retries after 1, 5 and 30 minutes (about 36 minutes in all); a window over 24 h is a posture warning. A 429's `Retry-After` lengthens a wait, never beyond the largest delay listed |
| `INKCAST_PUBLISH_RECOVERY_MAX_HOURS` | `24` | hours a post whose send was interrupted (the process stopped mid-send) stays `recovering` while the platform refuses to say whether it arrived (rate-limited, unreachable): asked again every 10 minutes and never sent meanwhile; `1` to `168`. Past the cap it is `unconfirmed` for a person to check, so a post is never sent late on its own. The `InkcastPostRecoveringTooLong` alert fires after an hour of it; a person can cancel a `recovering` post at any time, and the cancel wins over a late answer from the platform |
| `INKCAST_BACKUP_KEEP` | `14` | daily backups kept by the backup CronJob (`1` or more), and what the deletion pages tell a person: their data is erased from the database at once, but copies in backups age out within this many days, and a restored backup is erased again before it serves ([runbook](docs/runbooks/backup-restore.md)). The server reads the same name as the CronJob; change both together. Above `35` is a posture warning: a deleted person's data would linger over five weeks |
| `INKCAST_INSIGHTS_INTERVAL` | `10` | seconds between InsightsService passes that fold new events into KPI rollups (`1` or more: a fraction of a second is a tight loop) |
| `INKCAST_SHUTDOWN_GRACE` | `25` | seconds a stopping process gives in-flight requests and sends to finish: more than `0` and under `40`, the Deployment's `terminationGracePeriodSeconds`, at which point Kubernetes kills the pod whatever is in flight |
| `INKCAST_OUTBOX_RETENTION_DAYS` | `7` | days a domain event is kept in the outbox, counted from when it was written (the replay window), `1` to `30`. Purged by the scheduler's housekeeping once that old and every consumer has passed it: a consumer behind holds its events however old |
| `INKCAST_KPI_HOURLY_RETENTION_DAYS` | `90` | days hourly KPI buckets and their `kpi_applied` rows are kept, `31` to `400` (hourly series serve up to 31 days). Never shorter than `INKCAST_OUTBOX_RETENTION_DAYS`: `kpi_applied` is what stops a replayed event counting twice, so it must outlive the events. Daily buckets are kept indefinitely ([design](docs/design/observability-and-kpis.md#6-retention-privacy-and-gdpr)) |
| `INKCAST_KEYCLOAK_SYNC_CLIENT_ID` | | with `INKCAST_LOGIN=oidc` on Keycloak: the service-account client (`inkcast-sync`, `view-users` and `view-events` only) the user sync signs in as, so a person disabled or deleted in Keycloak loses their Inkcast tokens within a minute, and a person who deletes their own account is erased. Delete my account is refused until this is set. Its secret: `INKCAST_KEYCLOAK_SYNC_SECRET(_FILE)` |
| `INKCAST_KEYCLOAK_SYNC_INTERVAL` | `30` | seconds between reads of Keycloak's USER admin events and DELETE_ACCOUNT user events (`1` to `840`, so a reconcile can follow it: see the next row); `60` or more is a posture warning, since it breaks the "cut off within a minute" promise |
| `INKCAST_KEYCLOAK_RECONCILE_INTERVAL` | `600` | seconds between full re-reads of every Keycloak person Inkcast knows (also at start): from `INKCAST_KEYCLOAK_SYNC_INTERVAL` to `1680` less it. The `InkcastIdentitySyncPersonFailing` alert looks at a 30-minute window (`1800`), so a rarer reconcile could never raise it about a person who fails only there; the reconcile runs at the first pass after it is due (up to one sync interval late), the next one is due that long after the last *ends*, and Prometheus sees a failure at its next scrape, so 120 s of the window are kept free. `InkcastIdentitySyncStale` fires when no reconcile has read everyone it could for three times this value |
| `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` | `30` | days a person Keycloak no longer has (deleted by an administrator, or a self-deletion whose `DELETE_ACCOUNT` event the sync never saw) stays cut off before the reconcile erases their data, so an accidental deletion can be restored from a backup meanwhile. `0` erases at the first reconcile that finds them gone; `off` never erases on a 404 (cut off only), which the posture check warns about in production and staging. A disabled-but-present person is never erased |
| `INKCAST_KEYCLOAK_BREAKER` | `on` | the sync's circuit breaker: a reconcile that finds at least `INKCAST_KEYCLOAK_BREAKER_MIN` people, and more than `INKCAST_KEYCLOAK_BREAKER_PERCENT` of those it read, newly gone from Keycloak (404) or newly disabled in it (`enabled: false`) at once records the pass, raises `InkcastIdentitySyncBreakerOpen`, and holds everyone Keycloak says the same about until an operator runs `inkcast identity confirm-deletions --pass <id>` (Keycloak is right: exactly that pass's people are cut off; the gone start their erasure grace period, the disabled lose their personal access tokens) or `dismiss-deletions` (Keycloak was wrong: nothing is done, and the disabled are released). Held, a gone person is neither cut off nor erased and no grace period starts; a disabled person loses their OAuth tokens at once (a sign-in restores them) and keeps their personal access tokens, which answer `401 token_held` meanwhile. A Keycloak restored from an older backup, the wrong issuer or a switched-off LDAP provider all answer 404, or disabled when the provider imports, about people who exist. A self-deletion with its `DELETE_ACCOUNT` event is never held. `off` is a posture warning in production and staging ([runbook](docs/runbooks/alerts.md#inkcastidentitysyncbreakeropen)) |
| `INKCAST_KEYCLOAK_BREAKER_PERCENT` | `10` | the share (1–100) of the people read that must be newly gone or disabled, exceeded, for the breaker to trip |
| `INKCAST_KEYCLOAK_BREAKER_MIN` | `5` | the least number of people newly gone or disabled (1 or more) for the breaker to trip: both thresholds must be met, so one or two deletions or disables in a small realm are acted on as usual |
| `INKCAST_AUTHZ_ENGINE` | `local` | the permission engine ([design](docs/authorization.md)): `local` evaluates the model in-process over tuples in the Inkcast database; `openfga` asks an OpenFGA server. Production refuses `local` unless `INKCAST_AUTHZ_ALLOW_LOCAL=true` (a self-host that accepts it) |
| `INKCAST_FGA_API_URL`, `INKCAST_FGA_STORE_ID`, `INKCAST_FGA_MODEL_ID` | | with `openfga`: the server, the store and the pinned model version. The preshared key: `INKCAST_FGA_API_TOKEN(_FILE)`; `INKCAST_FGA_TIMEOUT` (`5`) seconds per request |
| `INKCAST_FGA_STORE_NAME` | | with `openfga` and both ids unset: the store to pin by name (3 to 64 characters, OpenFGA's rule). Start-up resolves that store and its model version equal to the packaged model, and refuses to start, naming the init container `authz-model` that writes them, until one exists. Both ids set win; the name never stands in for half of the pair. The Kustomize component sets `inkcast`, and `inkcast authz write-model` writes to the same name |
| `INKCAST_AUTHZ_MODEL` | the packaged model | with `local`: a `model.json` to evaluate instead of the one shipped in the package |
| `INKCAST_ENV` | `production` in the image | `production`/`staging` make the startup security checks strict; `ci`/`development` relax them. `inkcast checks` prints the table |
| `INKCAST_SECRETS_SOURCE` | `env` | where secrets come from: `env` (`NAME` or `NAME_FILE`), `dir` (a mounted Secret directory, `INKCAST_SECRETS_DIR`), `agent` (the AWS Secrets Manager agent), `aws`, `chain`. See [docs/reference/secrets.md](docs/reference/secrets.md) |
| `INKCAST_MAX_BODY_BYTES` | 10 MB | larger requests get 413 before any handler runs |
| `INKCAST_RATE_LIMIT_PER_MINUTE` | `60` | per-client limit on unauthenticated writes (`/register`, `/tokens/new`, sign-in, connect), `0` to `10000`; `0` turns it off, which with `INKCAST_LOGIN=password` is unlimited password guessing: a posture failure in production, a warning elsewhere. Above `600` (ten a second) with password sign-in is a posture warning: the limit no longer slows a guessing tool |
| `INKCAST_PAT_DEFAULT_DAYS` | `90` | days a personal access token lasts when the request doesn't say (`1` to `INKCAST_PAT_MAX_DAYS`) |
| `INKCAST_PAT_MAX_DAYS` | `366` | the most a request may ask for, `1` to `366`. Every entry point (`POST /api/v1/tokens`, `/tokens/new`, `inkcast_create_token`) refuses more with a 422 naming the limit. Lowering it doesn't shorten tokens already issued: they keep the expiry they were given |
| `INKCAST_PAT_ALLOW_NO_EXPIRY` | `true` | whether a token may never expire (REST `expires_in_days: null`, `/tokens/new?days=never`), to be revoked when no longer needed; `false` refuses it with a 422. Allowing it is a posture `info` in production and staging |
| `INKCAST_REFRESH_TOKEN_DAYS` | `30` | days a newly issued MCP refresh token lasts, `1` to `90`: an MCP client left unused that long signs in again (each refresh issues a new one with the full lifetime). Applies to tokens issued from then on. The access token's hour is not a setting |
| `INKCAST_ONE_TIME_LINK_MINUTES` | `15` | minutes a one-time link lasts, `5` to `60`, one value for both the connect link (`inkcast_connect`, `POST /api/v1/platforms/{id}/connect-link`) and the account-deletion link (`inkcast_delete_account`, `POST /api/v1/account/deletion-link`); the replies state it (`expires_in` seconds, "valid N minutes"). `60` is a hard maximum: a connect link is a bearer capability that passes through the AI transcript, and whoever holds it can attach their own platform account to the person's Inkcast. The sign-in and return windows are not settings |
| `INKCAST_CACHE_URL` | | `redis://host:6379/0` or `rediss://…` (Redis or Valkey) to share rate limits, locks and idempotency markers across replicas; empty keeps them in-process. The password goes in `INKCAST_CACHE_PASSWORD` or `INKCAST_CACHE_PASSWORD_FILE`, never in the URL |

## Development

```bash
python3 -m venv .venv    # every Python command runs from it: CONTRIBUTING.md, "Run the checks locally"
.venv/bin/pip install -e . 'ruff==0.12.0' 'playwright==1.56.0' && .venv/bin/python -m playwright install chromium
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -v
```

CI runs on every pull request ([docs/cicd.md](docs/cicd.md)):

- lint and the vendored-asset lock;
- the test suite on Python 3.12–3.15;
- rendering of every manifest overlay;
- an image build and vulnerability scan;
- a **kind** deployment of the real image under Pod Security `restricted`, both as one pod and as split services behind the Gateway;
- CodeQL.

Merges to main publish a signed image (cosign keyless), with an SBOM and SLSA provenance, and open a staging promotion PR.

The end-to-end test starts fake versions of a Keycloak-style identity provider, the Medium API and the DEV API. An unmodified MCP SDK client then:

1. Signs in the way Claude Code does: discovery, dynamic client registration, PKCE, then the consent page.
2. Connects both platforms through the connect page.
3. Cross-posts and schedules stories.
4. Runs the scheduler, including a rate-limit retry, a Medium-writer draft and a DEV organization post.
5. Lists and cancels posts.

The whole end-to-end suite runs twice: once in a single process, and once with every service in its own server behind the built-in Gateway.

Other tests cover:

- the REST API, OpenAPI document and docs pages, including a real-browser render under the CSP;
- duplicate-safe publishing (an ambiguous 5xx is never retried);
- Git publishing: personal access tokens and scopes, the ledger (create → unchanged → update or stale), idempotency, re-scheduling, the CLI and the browser token flow;
- architecture rules (no service imports another), the config gate, log redaction and secrets handling;
- probes and metrics;
- the vendored-asset lock.

Real Keycloak, Medium and DEV have not been exercised by the test suite yet. Try a `dry_run` and a draft first.

## Status

Early: v0.2 in progress. Services from one codebase, Git publishing, and SQLite (one node). The path to the hosted SaaS — Postgres, workspaces, an admin portal on Keycloak, API tokens on sign-up, a mobile app — is in [docs/product-spec.md](docs/product-spec.md) and [docs/issues/](docs/issues/).

## Documentation

Everything is indexed in [docs/README.md](docs/README.md): product spec, architecture, CI/CD, plug-in guide, roadmap and legal.

## License

Inkcast is **source-available**: [PolyForm Shield 1.0.0](LICENSE), plus an [additional permission](legal/ADDITIONAL-PERMISSIONS.md) for consultants.

| | Licence |
|---|---|
| Publishing **your own** content, self-hosting, modifying, building plug-ins | **Free** |
| Consultants setting it up for one client's own content | **Free** |
| Publishing **for clients** (agencies), selling Inkcast-powered services, embedding it in your product or SaaS, hosting or white-labelling it | **Commercial** |

Details and a quick check: [LICENSING.md](LICENSING.md). Contributions require the [CLA](legal/CLA-INDIVIDUAL.md). "Inkcast" is a trademark of ScalarOps Technologies LLC ([TRADEMARKS.md](TRADEMARKS.md)).

Copyright 2026 ScalarOps Technologies LLC.
