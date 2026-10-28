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
| `insights:read` | read your publishing KPIs: counts, success and on-time rates, Git sync outcomes |

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
- **Changed while the platform makes it wait** (it was rate-limited, and its `Retry-After` has not passed): the same post takes the change and keeps the wait. It goes out at the new time or when the wait ends, whichever is later; with the time removed or moved into the past, when the wait ends ([ADR-0017](adr/0017-a-post-waiting-for-its-retry-is-changed-in-place.md)).
- **No `publish_at`:** the file is published on the sync itself.

Publishing by hand (`inkcast_publish`, `POST /api/v1/posts`) is different: there a past time, or one under two minutes away, is refused; omit it to publish now.

## What each result means

| Result | Meaning |
|---|---|
| `create` | New: published, or scheduled at its `publish_at` (due at once if that has passed) |
| `update` | Changed: edited in place (DEV), or re-scheduled if it hadn't gone out yet |
| `unchanged` | Same content as last time: nothing sent |
| `stale` | Changed, but the platform can't edit posts (Medium). Edit it by hand at the URL shown; Inkcast won't post it again |
| `skipped` | The platform isn't connected, another sync of the file is running, or the file's post needs your decision: its last publish is `unconfirmed` (resend or cancel it, below), or its send was interrupted and Inkcast is still asking the platform whether it arrived (`recovering`; cancel it if you don't want it) |
| `failed` | Invalid file or platform error; the next sync tries again |

Files are matched to earlier posts by repository, path and platform. If you rename a file, it counts as a new story; to keep it linked, keep the path.

If a sync fails part-way (the request errors because Inkcast's database was busy, say), run it again:

- A post the failed run had already made is followed, never made a second time: the re-run reports its status. If that post is `unconfirmed` (the platform's answer to its send was lost), it waits for your decision, as above. Only a post the platform refused (`failed`) is made again, as after any `failed` result.
- A post the failed run had not made yet is made by the re-run. That is a new file's post, or the replacement of a scheduled post whose file you edited.
- If the failed run couldn't even record its error, the file stays held for up to 10 minutes. A re-run in that time reports `skipped` (another sync of the file is running); run it again after that.

## Any other CI, or your laptop

```bash
pip install "git+https://github.com/inkcasthq/inkcast"   # or use the container image
export INKCAST_URL=https://inkcast.example.com INKCAST_TOKEN=ink_pat_...
inkcast sync --dry-run 'posts/**/*.md'
inkcast sync 'posts/**/*.md'
```

The token is read only from the environment, never from an argument. Exit code `1` means something failed; `2` means a usage or permission problem.

## When Inkcast isn't sure a post went out

A publish can end `unconfirmed`: the platform's answer was lost after the request was sent, or the process was interrupted mid-send and the platform could not be asked whether the post arrived for `INKCAST_PUBLISH_RECOVERY_MAX_HOURS`. Inkcast never sends such a post again on its own, and a sync never creates it again: every push reports the file as `skipped` until you decide. The sync still asks the platform, and records the post if the platform has it.

- Check the platform first: the first send may have arrived.
- **Resend it** (`inkcast_resend`, or `POST /api/v1/posts/{id}/resend` with a `posts:write` token): one new post goes out now, the old one is cancelled, and the file follows the new post.
- **Cancel it** (`inkcast_cancel`, or `POST /api/v1/posts/{id}/cancel`): nothing is sent; an edit of the file is then published as a new post, so remove the first copy on the platform if it did arrive.

The sync result names the post id and both calls.

---
<!-- inkcast-nav -->
[Docs](README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)
