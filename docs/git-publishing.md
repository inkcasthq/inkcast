# Publishing from a Git repository

Keep your stories as markdown in Git. On every push, Inkcast publishes new files, updates changed ones in place where the platform allows it, and leaves unchanged files alone. **It never posts the same file twice.** On pull requests it shows what would happen.

## 1. Get a token

A personal access token lets CI sync stories, and do nothing else.

- **From Claude Code:** ask *"create an Inkcast token for my blog's CI"*. You get a link; open it, sign in, confirm, and copy the token shown. The token is never shown to the AI.
- **From the API** (with an OAuth token):
  ```bash
  curl -X POST https://inkcast.example.com/api/v1/tokens -H "Authorization: Bearer $OAUTH_TOKEN" \
       -H 'Content-Type: application/json' -d '{"name":"blog CI","scopes":["stories:sync"],"expires_in_days":90}'
  ```

| Scope | Allows |
|---|---|
| `stories:sync` | sync files (create, update, skip) |
| `posts:read` | list posts, previews, dry runs, the ledger |
| `posts:write` | publish, schedule and cancel single posts |
| `connections:read` / `connections:write` | see connected platforms; create connect links and disconnect |

Tokens can't create or revoke tokens. Revoke one with `DELETE /api/v1/tokens/{id}`, or ask Claude Code to.

## 2. Add the workflow

In your blog repository, add the secret `INKCAST_TOKEN` and the variable `INKCAST_URL`, then copy [`examples/publish-on-push.yml`](examples/publish-on-push.yml) to `.github/workflows/publish.yml`:

```yaml
- uses: inkcasthq/inkcast/action@<commit-sha>
  with:
    url: ${{ vars.INKCAST_URL }}
    token: ${{ secrets.INKCAST_TOKEN }}
    paths: "posts/**/*.md"
    comment: "true"          # preview comment on pull requests
```

| Input | Default | |
|---|---|---|
| `paths` | `posts/**/*.md` | space-separated files or globs |
| `platforms` | front matter | comma-separated override |
| `dry-run` | `auto` | `auto` means a dry run on pull requests and a real sync on pushes |
| `comment` | `false` | comment the preview on the pull request (needs `pull-requests: write`) |
| `fail-on-stale` | `false` | fail when a changed file couldn't be updated (Medium) |

Outputs: `results` (JSON) and `urls`. A table of results is added to the job summary.

## 3. Write stories

```markdown
---
title: Why we built an agent
subtitle: Lessons from year one
tags: [ai, agents, python]
canonical_url: https://blog.example.com/why-we-built-an-agent
platforms: [devto, medium]
publish_at: 2026-11-02T09:00:00-06:00   # optional; omit to publish on merge; up to 730 days ahead
devto:
  series: Building agents
---

The story. Relative images like ![diagram](img/flow.png) are rewritten to raw GitHub URLs at the commit.
```

### How `publish_at` is read

A file with a `publish_at` is always a scheduled post, at the time as written ([ADR-0015](adr/0015-publish-at-is-always-scheduled.md)):

- **In the future:** the post waits for that time, even if it is only seconds away when you push. It is never sent before it.
- **Already passed** (files are often re-read long after their date): the post is due at once, and the scheduler sends it at its next tick, with the usual retries if the platform is rate-limited or down.
- **Changed:** a time moved earlier or later re-schedules the post at the new time; a time removed makes it a "now" post, published on the next sync.
- **No `publish_at`:** the file is published on the sync itself.

Publishing by hand (`inkcast_publish`, `POST /api/v1/posts`) is different: there a past time, or one under two minutes away, is refused; omit it to publish now.

## What each result means

| Result | Meaning |
|---|---|
| `create` | New: published, or scheduled at its `publish_at` (due at once if that has passed) |
| `update` | Changed: edited in place (DEV), or re-scheduled if it hadn't gone out yet |
| `unchanged` | Same content as last time: nothing sent |
| `stale` | Changed, but the platform can't edit posts (Medium). Edit it by hand at the URL shown; Inkcast won't post it again |
| `skipped` | The platform isn't connected, another sync of the file is running, or a previous result is still unconfirmed |
| `failed` | Invalid file or platform error; the next sync tries again |

Files are matched to earlier posts by repository, path and platform. If you rename a file, it counts as a new story; to keep it linked, keep the path.

If a sync fails part-way (the request errors because Inkcast's database was busy, say), run it again: a post it had already made is picked up where it stands, never made a second time, and only a file that had no post yet is created afresh.

## Any other CI, or your laptop

```bash
pip install "git+https://github.com/inkcasthq/inkcast"   # or use the container image
export INKCAST_URL=https://inkcast.example.com INKCAST_TOKEN=ink_pat_...
inkcast sync --dry-run 'posts/**/*.md'
inkcast sync 'posts/**/*.md'
```

The token is read only from the environment, never from an argument. Exit code `1` means something failed; `2` means a usage or permission problem.

---
<!-- inkcast-nav -->
[Docs](README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)
