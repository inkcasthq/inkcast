# Handover and resume

Where the work stands and how to pick it up on another machine or in a fresh session, without the chat history. **Update this page at the end of every working session** (it's the hand-off, not a changelog: replace what's stale).

**As of 2026-10-27.**

## Where things stand

- **`main` is green.** The last round merged:
  - publishing fixes #189, #192, #194, #199;
  - database fixes #190, #193, #197, #208;
  - Keycloak fixes #191, #195, #198;
  - always-schedule (#217, [ADR-0015](adr/0015-publish-at-is-always-scheduled.md)), recovering posts (#221), double-send (#223), person decides (#231);
  - the mockup standard and tooling (#236, [ADR-0016](adr/0016-mockup-standard-and-frontend-targets.md));
  - the tokensave dashboard design and mockups (#237);
  - the Dependabot bumps #170 (Prometheus v3.15.1) and #171 (actions, with the CodeQL bundle v2.27.3 in `scripts/dev/codeql.sh`).
- **Inkcast since then:** `settings.py apply` finishes and names the organisation policy that blocks Actions PRs (#242); the commit hashes the docs cite were updated (#245).
- **tokensave fork (#227), [`ephico2real2/tokensave`](https://github.com/ephico2real2/tokensave):** `master` is at `2bd59fb`, CI green on Linux, Windows and macOS 15. On it:
  - S1, per-turn detail and subagent runs (#246, fork PR 1);
  - the fixes #250 (every line of a message merged into its turn), #251 (tests kept out of `~/.tokensave`), #254 (concurrent opens), #255 (tool results counted once), #256 (same-second reads), #257 (CI on `macos-15`; `macos-14` retires 2026-11-02);
  - #248, `global.db` schema versions (`PRAGMA user_version`, numbered migrations; a newer database opens read only);
  - #249, `tokensave accounting backfill [--dry-run]`;
  - #252, turns whose model had no price recorded as unpriced and priced by the next ingest, never shown as $0.
- **Measured on a copy of the owner's `global.db`** (the PRs hold the SQL): the backfill fills detail on 75,695 turns; #252 prices the 75,783 Claude turns stored at $0, and the Claude total goes from $176.77 to $9,680.75.
- **Nothing is left half-built.** Every branch worth keeping is merged; stale local worktrees can be deleted.

## Decisions taken this round

| Topic | Decision | Where |
|---|---|---|
| `publish_at` in a Git file | Always a scheduled post, at the time as written; lag measured from `MAX(run_at, created_at)` | ADR-0015 |
| Posts stuck recovering | Cancel, alert after 1 h, cap at `INKCAST_PUBLISH_RECOVERY_MAX_HOURS` (24) | #221 |
| Unconfirmed posts past the cap | The person decides: cancel, or resend (`inkcast_resend`, `POST /api/v1/posts/{id}/resend`); Git sync never re-creates them | #231 |
| Mockups | One app per mockup, one token table, typed datasets, fixed section types, checked at 390/820/1440 px | ADR-0016, [mockup standard](tooling/mockup-standard.md) |
| Frontends | Inkcast stays React + TypeScript; small pages embedded in a binary use Preact + TypeScript | ADR-0016 |
| tokensave dashboard | A fork with workspace discovery, agent/model/effort discovery and a local dashboard, dark by default | #227, [design](tooling/tokensave-dashboard/design.md) |
| tokensave `global.db` schema | Versioned by `PRAGMA user_version`; each migration in its own `BEGIN IMMEDIATE`; a database newer than the binary is read only | #248 |
| tokensave turns with no price | Stored as unpriced (`NULL`), shown as `$1.23+` / `unpriced` with a count; every ingest prices those whose model gained a price. Ingestion does not fetch prices (the Stop hook is on the session-end path); `tokensave cost` and `tokensave gain` do | #252 |
| tokensave fork merges | A stack of PRs is merged by fast-forwarding `master` after one integration PR runs CI on the whole stack | fork PR 10 |

## How the work is done

1. **Issue → branch → PR → green → squash merge.** One issue per PR; the PR body says `Fixes #N` (or `Refs #N` when the issue stays open). Bugs found along the way get their own issue.
2. **Roles.** Builders work at high effort, extra-high for shared or central services. Every central-service PR gets an adversarial review that *proves* each finding: a failing test plus a minimal fix, one commit per finding. Each finding is checked (fails before, passes after) before it's merged.
3. **Gates**, all before pushing:
   ```bash
   PYTHONPATH=src:tests python3 -m unittest discover -s tests
   ruff check .
   python3 scripts/gen-manifests.py --check && python3 scripts/gen-alerts.py --check && python3 scripts/gen-dashboards.py --check
   scripts/dev/codeql.sh     # must end "nothing at CI's failure threshold"
   ```
   Mockups: `scripts/mockups/check.py <Main.dc.html> --out <dir>` must report 0 problems.
4. **Commits** are authored as `ephico2real2` with the GitHub noreply address, and carry no tool trailers.
5. **Commit and push often.** An unpushed branch is lost work.
6. **Repo rules:** keep the public repo generic (no brand or customer names); platform credentials never pass through chat or AI tool output; diagrams, pages and screenshots are light theme only.

**tokensave fork gates** (in `~/gitRepos/tokensave`, toolchain 1.98.1):
```bash
cargo fmt --check
cargo clippy --workspace --all-targets --locked     # no location beyond the 8 already on master
cargo test --workspace --locked --features test-transport --no-fail-fast
```
Every new or changed SQL statement goes into `docs/QUERY-REPORT.md`, its citations checked against the source. Tests and probes never write the real `~/.tokensave` or `~/.claude`: use a scratch `TOKENSAVE_HOME` with `TOKENSAVE_SKIP_AGENT_MAINTENANCE=1`, and read the live database only through `sqlite3 ~/.tokensave/global.db ".backup <scratch file>"`.

**Practical notes.**
- Read CI failures from the check-run annotations: `gh api repos/inkcasthq/inkcast/check-runs/<id>/annotations`.
- Merge with REST when GraphQL isn't available: `gh api -X PUT repos/inkcasthq/inkcast/pulls/<n>/merge -f merge_method=squash -f sha=<head sha>`.
- Before opening a PR, `git diff origin/main...HEAD --stat` must list only the story's files.
- A Dependabot image bump must land in every place the image is pinned (`tests/test_architecture.py` checks). A `github/codeql-action` bump also needs `CODEQL_BUNDLE` and its SHA-256s in `scripts/dev/codeql.sh`: the bundle is `bundleVersion` in the action's `src/defaults.json` at the pinned commit.

## Owner's steps on the Mac

1. **Set up the Mac:** clone into `<parent>/gitRepos/inkcast` and run the checks above (#66 tracks the pinned macOS tools and a doctor script).
2. **Apply the repository settings** (squash merges take the PR title only; #142, #143). The organisation must first allow Actions to create pull requests, or `apply` is refused with HTTP 409 (#240). In order:
   ```bash
   gh auth refresh -h github.com -s admin:org
   gh api -X PUT orgs/inkcasthq/actions/permissions/workflow \
     -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true
   scripts/repo/settings.py apply
   ```
   **Done 2026-10-27:** `scripts/repo/settings.py check` prints "repository settings match the files", and the `main` ruleset is active.
3. **Attribution clean-up** (the owner's call):
   - strip the tool-appended "Generated by" line from old PR and issue bodies;
   - decide whether to rewrite the early history of `main` (PR #4 and earlier) to drop tool trailers and re-author early commits. This needs a force-push and is destructive.
4. **tokensave:** the Claude Code hooks and the MCP server run `~/.cargo/bin/tokensave`, which was built on 2026-10-27 at 05:25 from S1 only: the live `global.db` is still at schema version 0, without the #248–#257 changes. `tokensave` on `PATH` is Homebrew 7.16.0 (`~/.cargo/bin` isn't on `PATH`).
   - Install the fork's `master` again. Its first open moves `global.db` to schema version 2 by rebuilding `turns`; on a copy, the first Stop hook (debug build, with the migration and the repricing) took 4.06 s.
   - Run `tokensave accounting backfill --dry-run`, then `tokensave accounting backfill`, then `tokensave cost` (it fetches prices; the ingest before the report prices the unpriced turns).
   - Decide whether to keep Homebrew 7.16.0 on `PATH`. It writes to `global.db` only from `tokensave cost`, `discover` and `monitor`. Both binaries share `parse_offsets`, and 7.16.0 inserts with `INSERT OR IGNORE`, so a turn it reads first has no S1 detail and, if its model has no price, stays at $0 (#252 reprices only unpriced rows). Recommended: `brew unlink tokensave` (undone by `brew link tokensave`) and add `~/.cargo/bin` to `PATH`. Measured 2026-10-27: the 1,846 Claude turns since 05:25 all have `effort` set, so 7.16.0 had written none of them.

## Open issues by area

| Area | Issues |
|---|---|
| Git sync | #204, #224, #229, #230 |
| Publishing | #130, #214, #222, #234 (item 7 needs the owner's call) |
| Erasure, export and restore | #61, #112, #128, #161, #202, #212 |
| Identity, authorization and limits | #42, #72, #136, #139, #215, #219 |
| Observability | #40, #207 |
| Platform, repo and CI | #32, #34, #38, #45, #59, #66, #141, #149, #159 |
| Deployment | #39 |
| Developer tooling | #227 (S2–S6 to build), #246 (S1: merged as fork PR 1; its checklist still to be ticked against the PR's evidence, then closed) |

**Suggested order:**
1. The Git sync and publishing follow-ups, because they guard double-sends and lost posts.
2. Then erasure (#212, #112).
3. Then the repo hardening (#34, #45), once the settings are applied.
4. tokensave: S2 (workspace) next; S3 (dashboard) after the owner reviews the mockups.

**Unanswered question:** may the tokensave dashboard's own repo carry dark screenshots, since its default theme is dark? Inkcast's stay light.

## Resume prompt

Paste this into a new session, in the repo:

> Read `docs/handover.md`, `CONTRIBUTING.md`, `docs/contributor-map.md` and `docs/definition-of-done.md`. Check `main` is green and list open PRs. Then continue with the suggested order in the handover, one issue per PR, following "How the work is done". Update `docs/handover.md` at the end of the session.

---
<!-- inkcast-nav -->
[Docs](README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)
