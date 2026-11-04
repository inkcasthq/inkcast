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
| `stories:sync` | sync files (create, update, skip; cancel a removed file's pending post) |
| `posts:read` | list posts, previews, dry runs, the ledger |
| `posts:write` | publish, schedule, cancel and resend single posts |
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
| `fail-on-decision` | `false` | fail while a file's post waits for your decision (`unconfirmed` or `recovering`, [below](#when-inkcast-isnt-sure-a-post-went-out)); every push fails until you resend or cancel it, or edit a file whose old entry has no post to resend or cancel |

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

Front matter must be values JSON can carry. A file with any of these reports `failed`, with the field named, until you edit it:

- in a platform block (`devto:`), a key that isn't text: `1:`, or `on:` and `yes:`, which YAML reads as true; quote it (`"on":`);
- in a platform block, `.nan`, `.inf` or `-.inf`, binary data (`!!binary`) or a set (`!!set`);
- a tag that isn't a name, such as a list inside `tags` (`tags: [[a, b]]`, or the same in `devto.tags`);
- anywhere, an unpaired surrogate (`"\ud83d"`); write the character itself, not its escapes.

### How `publish_at` is read

A file with a `publish_at` is always a scheduled post, at the time as written ([ADR-0015](adr/0015-publish-at-is-always-scheduled.md)):

- **In the future:** the post waits for that time, even if it is only seconds away when you push. It is never sent before it.
- **Already passed** (files are often re-read long after their date): the post is due at once, and the scheduler sends it at its next tick, with the usual retries if the platform is rate-limited or down. Such a post is a backfill ([ADR-0019](adr/0019-a-backfill-post-has-no-deadline.md)): it goes out after every post whose time has come, in the order the files were synced, 20 a tick, so a large import of old files drains over minutes without delaying anything you scheduled. It counts in your `posts_scheduled` and `posts_published` and, having no time to be on time for, in neither `posts_on_time` nor `posts_late`. A file pushed after the time it names is a backfill too.
- **Changed:** a time moved earlier or later re-schedules the post at the new time; a time removed makes it a "now" post, published on the next sync.
- **Changed while the platform makes it wait** (it was rate-limited, and its `Retry-After` has not passed): the same post takes the change and keeps the wait. It goes out at the new time or when the wait ends, whichever is later; with the time removed or moved into the past, when the wait ends ([ADR-0017](adr/0017-a-post-waiting-for-its-retry-is-changed-in-place.md)).
- **No `publish_at`:** the file is published on the sync itself.
- **Changed on a post you cancelled by hand:** a push that changes only the file's `publish_at` is reported `unchanged`; change the content too, and the file is published as a new post at the new time (an open exception of [ADR-0015](adr/0015-publish-at-is-always-scheduled.md)).

Publishing by hand (`inkcast_publish`, `POST /api/v1/posts`) is different: there a past time, or one under two minutes away, is refused; omit it to publish now.

## What each result means

| Result | Meaning |
|---|---|
| `create` | New: published, or scheduled at its `publish_at` (due at once if that has passed) |
| `update` | Changed: edited in place (DEV), or re-scheduled if it hadn't gone out yet |
| `unchanged` | Same content as last time: nothing sent |
| `stale` | Changed, but the platform can't edit posts (Medium). Edit it by hand at the URL shown; Inkcast won't post it again |
| `skipped` | The platform isn't connected, another sync of the file is running, or the file's post needs your decision: its last publish is `unconfirmed` (resend or cancel it, below), or its send was interrupted and Inkcast is still asking the platform whether it arrived (`recovering`; cancel it if you don't want it) |
| `failed` | Invalid file or platform error; the next sync tries again. A file that fails validation changes nothing: a post its last valid version scheduled stays and goes out at its time, and the message names it |
| `removed` | The file was removed from the repository: its pending post was cancelled, or, if it had been published, left on the platform (the status and message say which) |

Files are matched to earlier posts by repository, path and platform. If you rename a file, it counts as a new story; to keep it linked, keep the path.

### Removing a file

A push that deletes a file cancels the file's pending post: one that is `scheduled`, or `recovering` or `unconfirmed` (its send was interrupted, or the platform's answer was lost; it may already be on the platform, and the result says so, as a cancel by hand does). A published post stays on the platform: Inkcast never deletes there; remove it by hand if you want it gone. A removal cancels only posts that came from that repository's sync (matched by repository, path and platform), never a post published by hand or from another repository. A renamed file is a removal and a new story. If you put the file back at the same path, a file whose `scheduled` post was cancelled is published as a new post; one whose post may already be on the platform (`recovering`, `unconfirmed`) is published again only once you change it, as after a cancel by hand, so a revert never sends a second copy.

The action works this out from the push itself: the files deleted between the commit before the push and the pushed commit (`git diff --no-renames --diff-filter=D`), within `paths`. The first push of a branch has no commit before it, so it reports no removals; on a pull request the dry run says which posts the merge would cancel. From any other CI, or a laptop, name the base revision yourself:

```bash
inkcast sync --removed-since 1a2b3c4 'posts/**/*.md'   # the commit the last sync ran at
```

If the revision isn't in the clone (a shallow checkout), the command stops with exit code `2` and says so; fetch it, or sync without the option.

If a sync fails part-way (the request errors because Inkcast's database was busy, say), run it again:

- A post the failed run had already made is followed, never made a second time: the re-run reports its status. If that post is `unconfirmed` (the platform's answer to its send was lost), it waits for your decision, as above. Only a post the platform refused (`failed`) is made again, as after any `failed` result.
- A post the failed run had not made yet is made by the re-run. That is a new file's post, or the replacement of a scheduled post whose file you edited, which keeps the replaced post's place in the queue as the first run would have.
- If the failed run couldn't even record its error, the file stays held for up to 10 minutes. A re-run in that time reports `skipped` (another sync of the file is running); run it again after that.

A re-run sends the same request, so the same `Idempotency-Key`, and the server tells the two kinds apart. A result that asked for a re-run (`skipped`: the file held by another sync, its post being sent, or its state changed during the sync; `failed`: the next sync tries again, after you reconnect a platform that refused its key, say) isn't kept for replay: the re-run runs, and a removal whose cancel lost a race cancels the post then. A result with nothing skipped or failed is replayed as it was for 24 hours (`Idempotent-Replayed: true`). Either way nothing is sent twice: the ledger remembers what each run made.

## Any other CI, or your laptop

```bash
pip install "git+https://github.com/inkcasthq/inkcast"   # or use the container image
export INKCAST_URL=https://inkcast.example.com INKCAST_TOKEN=ink_pat_...
inkcast sync --dry-run 'posts/**/*.md'
inkcast sync 'posts/**/*.md'
```

The token is read only from the environment, never from an argument. Exit code `1` means something failed; `2` means a usage or permission problem. A file whose post waits for your decision (below) is `skipped` and exits `0`: only you can resolve it, and a run that failed on every push until you did would be noise in most repositories. If you want CI to fail until the decision is made, pass `--fail-on-decision` (the action's `fail-on-decision: "true"`).

## When Inkcast isn't sure a post went out

A publish can end `unconfirmed`: the platform's answer was lost after the request was sent, or the process was interrupted mid-send and the platform, asked for `INKCAST_PUBLISH_RECOVERY_MAX_HOURS` and one last time at the end of them, never said it had the post. Inkcast never sends such a post again on its own, and a sync never creates it again: every push reports the file as `skipped` until you decide. The sync still asks the platform, and records the post if the platform has it.

- Check the platform first: the first send may have arrived.
- **Resend it** (`inkcast resend <post-id>` from the CLI, `inkcast_resend`, or `POST /api/v1/posts/{id}/resend`, with a `posts:write` token): one new post goes out now, the old one is cancelled, and the file follows the new post. The platform must be connected: a resend while it isn't, or while it refuses the stored credentials, is refused (`409 not_connected`) before anything is cancelled, and the reply names the reconnect step. A retried resend (its first reply lost) is refused too (`409 not_resendable`), and the reply names the post the first one made (`post_id` in the body), so follow that one.
- **Cancel it** (`inkcast cancel <post-id>`, `inkcast_cancel`, or `POST /api/v1/posts/{id}/cancel`): nothing is sent; an edit of the file is then published as a new post, so remove the first copy on the platform if it did arrive.

A file whose entry is from before Inkcast recorded post ids (`unconfirmed`, naming no post) has no post to resend or cancel. It is `skipped` on every push, on every platform, until you edit it: the edit is published as a new post, so check the platform first and remove the first copy if it did arrive. The sync still asks the platform, and records the post if the platform has it; it never sends such a file again on its own, whatever the platform says.

The sync result names the post id and both calls. From the CLI, with a `posts:write` token in `INKCAST_TOKEN`:

```bash
inkcast resend pst_0123456789ab     # published  https://dev.to/...  (post pst_...); exit 1 if the new post isn't published
inkcast cancel pst_0123456789ab     # cancelled  (post pst_...), with the note when it may already be on the platform
```

---
<!-- inkcast-nav -->
[Docs](README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)
