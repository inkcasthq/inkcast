# Inkcast

**Write once in markdown. Publish everywhere — from your AI assistant.**

Inkcast is an MCP server that lets Claude Code, or any MCP client, publish, cross-post and schedule your writing on Medium, DEV and more. You manage it all from the terminal, with a canonical link back to your own blog on every copy.

```text
> Publish docs/blog/why-we-built-an-agent.md to Medium and DEV with the subtitle
  "Lessons from year one" and the canonical URL pointing to our blog

● inkcast_publish (dry run)  → per-platform preview checked
● inkcast_publish            → “Why we built an agent” — Lessons from year one
  - medium: published → https://medium.com/@ada/why-we-built-an-agent-…
  - devto: published → https://dev.to/ada/why-we-built-an-agent-…
```

## Why it's built this way

| Principle | What it means in practice |
|---|---|
| **Plug-in platforms** | Each platform is a class behind one small interface. Medium and DEV ship built in; others install as Python packages through the `inkcast.platforms` entry point. See [docs/writing-a-platform-plugin.md](docs/writing-a-platform-plugin.md). |
| **Pluggable sign-in** | OpenID Connect (Keycloak recommended), a GitHub OAuth app, or a single password for local use. With Keycloak, Google/GitHub/Microsoft social login and account linking happen in Keycloak, not in Inkcast. |
| **Secrets never touch the AI** | Platform keys are entered on a one-time browser page, never pasted into chat. They are encrypted at rest. OAuth codes and tokens are stored only as SHA-256 hashes. |
| **Explicit consent** | Every MCP client that registers itself is shown a consent page before it gets a token. Codes are single-use; refresh tokens rotate. |
| **Platform-correct output** | Each plug-in knows its platform's rules. Medium needs the title and subtitle inside the body and uses only 3 tags. DEV wants lowercase alphanumeric tags and no title in the body. Writers in a Medium publication can only create drafts. |
| **Own scheduler** | Scheduled posts live in Inkcast's database and go out on time. Rate limits and server errors are retried automatically. |

More detail on the design is in [docs/architecture.md](docs/architecture.md).

## Platforms

| Platform | Status | Credentials | Notes |
|---|---|---|---|
| Medium | ✅ built in | Integration token | Medium stopped issuing new tokens on 1 Jan 2025; tokens created earlier still work. Without one, Inkcast gives you Medium's *Import a story* steps instead. That import also sets the canonical link. |
| DEV (dev.to) | ✅ built in | API key | Works with any Forem community via `INKCAST_API_URL_DEVTO`. |
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

## Quick start (local, single user)

```bash
pip install .            # Python ≥ 3.11
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
4. Optional social login: **Identity providers → Google / GitHub / Microsoft…**. Keycloak's *first broker login* flow links a social account to an existing user with the same verified email. Inkcast always sees one stable user ID per person.

### 2. Run Inkcast

```bash
cp .env.example .env    # fill in INKCAST_PUBLIC_URL, INKCAST_SECRET_KEY, OIDC_*
docker build -t inkcast .
docker run -d --name inkcast --env-file .env -v inkcast-data:/data -p 8787:8787 inkcast
```

Put it behind HTTPS (any reverse proxy or ingress). Then everyone adds it with:

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
publish_at: 2026-10-21T09:00:00-05:00        # omit to publish now; a UTC offset is required
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

All settings are environment variables; see [.env.example](.env.example).

## Development

```bash
pip install -e .
PYTHONPATH=src:tests python -m unittest discover -s tests -v
```

The end-to-end test starts fake versions of a Keycloak-style identity provider, the Medium API and the DEV API. An unmodified MCP SDK client then:

1. Signs in the way Claude Code does: discovery, dynamic client registration, PKCE, then the consent page.
2. Connects both platforms through the connect page.
3. Cross-posts and schedules stories.
4. Runs the scheduler, including a rate-limit retry, a Medium-writer draft and a DEV organization post.
5. Lists and cancels posts.

Real Keycloak, Medium and DEV have not been exercised by the test suite yet. Try a `dry_run` and a draft first.

## Status

Early: v0.1, single node, SQLite. The path to the hosted SaaS — Postgres, workspaces, an admin portal on Keycloak, API tokens on sign-up, a mobile app — is in [docs/product-spec.md](docs/product-spec.md) and [docs/issues/](docs/issues/).
