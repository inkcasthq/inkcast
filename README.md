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
| `inkcast_posts` | Lists your scheduled, published, draft, failed and cancelled posts. |
| `inkcast_cancel` | Cancels a scheduled post. |
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
pip install .            # Python ≥ 3.12 (the image runs 3.14)
export INKCAST_LOGIN=password INKCAST_PASSWORD='a long passphrase here' \
       INKCAST_SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')
inkcast                  # serves http://localhost:8787/mcp
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

```bash
oc new-project inkcast
oc create secret generic inkcast-secrets \
  --from-literal=secret-key="$(openssl rand -base64 48)" --from-literal=oidc-client-secret=…
# edit deploy/kustomize/base/kustomization.yaml (public URL, OIDC issuer, image) and the Route host, then:
oc apply -k deploy/kustomize/overlays/openshift          # or: kubectl apply -k deploy/kustomize/overlays/kubernetes
```

**What the manifests set up:**

- **Pod security:** restricted, i.e. arbitrary non-root UID, read-only root filesystem, no capabilities.
- **Network:** default-deny NetworkPolicy, plus optional FQDN egress allow-lists (Cilium, or OVN EgressFirewall).
- **Probes and metrics:** `/livez` and `/readyz` probes, with Prometheus metrics on a separate, unrouted port.
- **Exposure:** an edge-TLS Route with HSTS on OpenShift, or a Gateway API HTTPRoute on Kubernetes.

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
| `INKCAST_OVERDUE_GRACE` | `600` | seconds past due before a scheduled post counts as overdue (metric and alert) |
| `INKCAST_PUBLISH_MAX_ATTEMPTS` | `4` | sends in all for a scheduled post whose send certainly never happened (a connection failure, a 429) before it is marked `failed`; `1` to `10`. A send that may have gone out (a timeout after sending, a 5xx) is never retried: the post is `unconfirmed` for a person to check |
| `INKCAST_PUBLISH_RETRY_DELAYS` | `60,300,1800` | seconds between those sends, comma-separated: 1 to 9 whole numbers, each `30` to `21600`, never decreasing; the last one repeats. The default retries after 1, 5 and 30 minutes (about 36 minutes in all); a window over 24 h is a posture warning |
| `INKCAST_BACKUP_KEEP` | `14` | daily backups kept by the backup CronJob, and what the deletion pages tell a person: their data is erased from the database at once, but copies in backups age out within this many days, and a restored backup is erased again before it serves ([runbook](docs/runbooks/backup-restore.md)). The server reads the same name as the CronJob; change both together |
| `INKCAST_INSIGHTS_INTERVAL` | `10` | seconds between InsightsService passes that fold new events into KPI rollups |
| `INKCAST_KEYCLOAK_SYNC_CLIENT_ID` | | with `INKCAST_LOGIN=oidc` on Keycloak: the service-account client (`inkcast-sync`, `view-users` and `view-events` only) the user sync signs in as, so a person disabled or deleted in Keycloak loses their Inkcast tokens within a minute, and a person who deletes their own account is erased. Delete my account is refused until this is set. Its secret: `INKCAST_KEYCLOAK_SYNC_SECRET(_FILE)` |
| `INKCAST_KEYCLOAK_SYNC_INTERVAL` | `30` | seconds between reads of Keycloak's USER admin events and DELETE_ACCOUNT user events |
| `INKCAST_KEYCLOAK_RECONCILE_INTERVAL` | `600` | seconds between full re-reads of every Keycloak person Inkcast knows (also at start) |
| `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` | `30` | days a person Keycloak no longer has (deleted by an administrator, or a self-deletion whose `DELETE_ACCOUNT` event the sync never saw) stays cut off before the reconcile erases their data, so an accidental deletion can be restored from a backup meanwhile. `0` erases at the first reconcile that finds them gone; `off` never erases on a 404 (cut off only), which the posture check warns about in production and staging. A disabled-but-present person is never erased |
| `INKCAST_KEYCLOAK_BREAKER` | `on` | the sync's circuit breaker: a reconcile that finds at least `INKCAST_KEYCLOAK_BREAKER_MIN` people, and more than `INKCAST_KEYCLOAK_BREAKER_PERCENT` of those it read, gone from Keycloak at once cuts off and erases nobody and starts no grace period; it records the pass, raises `InkcastIdentitySyncBreakerOpen`, and holds everyone Keycloak says is gone until an operator runs `inkcast identity confirm-deletions --pass <id>` (they really are gone: exactly that pass's people are cut off and their grace period starts) or `dismiss-deletions` (Keycloak was wrong: nothing is done). A Keycloak restored from an older backup, the wrong issuer or a disabled LDAP provider all answer 404 about people who exist. A self-deletion with its `DELETE_ACCOUNT` event is never held. `off` is a posture warning in production and staging ([runbook](docs/runbooks/alerts.md#inkcastidentitysyncbreakeropen)) |
| `INKCAST_KEYCLOAK_BREAKER_PERCENT` | `10` | the share (1–100) of the people read that must be newly gone, exceeded, for the breaker to trip |
| `INKCAST_KEYCLOAK_BREAKER_MIN` | `5` | the least number of people newly gone (1 or more) for the breaker to trip: both thresholds must be met, so one or two deletions in a small realm are acted on as usual |
| `INKCAST_AUTHZ_ENGINE` | `local` | the permission engine ([design](docs/authorization.md)): `local` evaluates the model in-process over tuples in the Inkcast database; `openfga` asks an OpenFGA server. Production refuses `local` unless `INKCAST_AUTHZ_ALLOW_LOCAL=true` (a self-host that accepts it) |
| `INKCAST_FGA_API_URL`, `INKCAST_FGA_STORE_ID`, `INKCAST_FGA_MODEL_ID` | | with `openfga`: the server, the store and the pinned model version. The preshared key: `INKCAST_FGA_API_TOKEN(_FILE)`; `INKCAST_FGA_TIMEOUT` (`5`) seconds per request |
| `INKCAST_AUTHZ_MODEL` | the packaged model | with `local`: a `model.json` to evaluate instead of the one shipped in the package |
| `INKCAST_ENV` | `production` in the image | `production`/`staging` make the startup security checks strict; `ci`/`development` relax them. `inkcast checks` prints the table |
| `INKCAST_SECRETS_SOURCE` | `env` | where secrets come from: `env` (`NAME` or `NAME_FILE`), `dir` (a mounted Secret directory, `INKCAST_SECRETS_DIR`), `agent` (the AWS Secrets Manager agent), `aws`, `chain`. See [docs/reference/secrets.md](docs/reference/secrets.md) |
| `INKCAST_MAX_BODY_BYTES` | 10 MB | larger requests get 413 before any handler runs |
| `INKCAST_RATE_LIMIT_PER_MINUTE` | `60` | per-client limit on unauthenticated writes (`/register`, `/tokens/new`, sign-in, connect); `0` turns it off |
| `INKCAST_CACHE_URL` | | `redis://host:6379/0` or `rediss://…` (Redis or Valkey) to share rate limits, locks and idempotency markers across replicas; empty keeps them in-process. The password goes in `INKCAST_CACHE_PASSWORD` or `INKCAST_CACHE_PASSWORD_FILE`, never in the URL |

## Development

```bash
pip install -e . 'playwright==1.56.0' && python -m playwright install chromium
PYTHONPATH=src:tests python -m unittest discover -s tests -v
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
