# Handover and resume

Where the work stands and how to pick it up on another machine or in a fresh session, without the chat history. **Update this page at the end of every working session** (it's the hand-off, not a changelog: replace what's stale).

**As of 2026-11-05.**

## Where things stand

- **The work follows one order.** [`docs/backlog/order.yaml`](backlog/order.yaml) puts every open story and issue into waves, and [`docs/backlog/TRACKER.md`](backlog/TRACKER.md) is generated from it (`scripts/backlog/tracker.py`; CI fails when the tracker is stale). Each wave is reported on [#360](https://github.com/inkcasthq/inkcast/issues/360).
- **W1, "Finish what is in flight":** F3.1.S9 (the SQLite backups), F3.9.S5 (staging and production on OpenShift Local, OpenFGA on one shared CloudNativePG), F3.9.S8 (OpenFGA's migration as an init container), F9.1.S3 (the Keycloak production component) and F1.8.S1 (the `main` ruleset) are done. Two stories wait on the owner (see "Owner's steps"):
  - F1.8.S5 (#38): every pull request's `CodeQL` check still warns "1 configuration not found" for `.github/workflows/publish.yml:codeql`;
  - F1.8.S6 (#45): the security settings are on and `scripts/repo/settings.py check` matches, but the daily `repo-settings` workflow can't see them with its token (#540).
- **W2, "Security, publishing and Git sync safety":** worked down twice. Its issues closed on 2026-11-04, then the review follow-ups that took their place closed on 2026-11-05; the backlog PRs #448, #483, #488 and #512 record each step. The follow-ups now in W2 are #523, #531, #533 and #535; #522 is in W17.
- **Deployed on the lab's OpenShift Local** (by digest from `main`'s overlays; `~/.claude/skills/inkcast-crc/check.sh` reports ALL PASS after each deploy):
  - `inkcast-staging`: `sha256:f66c89b7d7ca` (`main` at `5cefe3a6`);
  - `inkcast-prod`: `sha256:844b5ccd95ed`.
- **Next:** W3, the writer web app, starting with F4.1.S5 (#348, the Node lock resolved in CI), then the stories in the order W3 lists.

## Decisions taken since 2026-10-27

| Topic | Decision | Where |
|---|---|---|
| Builders and reviewers | Opus 5.5 builds and reviews while the Fable limit is spent; Codex reviews too. Each finding is checked by the integrator (fails before, passes after) before it merges. Work to be reviewed again by Fable carries the `fable-review-later` label | 2026-11-04 |
| A deleted linked directory or submodule | The CLI sends the removed directories and the patterns (`removed_dirs`, `removed_globs`); the server cancels the posts its own ledger synced below them, under the `stories:sync` scope a sync token already has. A pattern's matching cost is bounded, since the server runs patterns a request names | #508, PR #536 |
| Second-copy notes on an update | Recognised by the exact wording their builders write, in linear time (the earlier pattern took 8 s on a 53 KB note), and kept in order | #504, PR #534 |
| OpenFGA's migration | Runs as an init container (no Job to delete and re-apply); an eviction fence ends a paused earlier runner's `openfga migrate` | F3.9.S8 PR #374; #494 PR #521 |
| The image | No package manager at run time, so apt's CVE-2026-109518 isn't shipped | #525, PR #526 |
| Keycloak in production | The production component runs on CloudNativePG; the lab's own Keycloak is not moved by it (the owner's decision) | F9.1.S3, PR #391 |
| Promotions | `promote.yml` closes the promotion PRs a newer digest supersedes, and a promotion merge publishes nothing | PRs #307, #383, #518 |
| Repository settings check | A value GitHub doesn't show the checking token is NOT VERIFIED, never drift | #540 |

Earlier decisions (publishing, recovery, mockups, frontends) are in the ADRs, [`docs/adr/`](adr/).

## How the work is done

1. **Issue → branch → PR → green → squash merge.** One issue per PR; the PR body says `Fixes #N` (or `Refs #N` when the issue stays open). Bugs found along the way get their own issue, placed in `order.yaml`.
2. **Roles.** Builders work at high effort, extra-high for shared or central services. Every central-service PR gets an adversarial review that *proves* each finding: a failing test plus a minimal fix, one commit per finding. Each finding is checked (fails before, passes after) before it's merged.
3. **Gates**, all before pushing:
   ```bash
   PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests
   .venv/bin/ruff check .
   .venv/bin/python scripts/gen-manifests.py --check && .venv/bin/python scripts/gen-alerts.py --check && .venv/bin/python scripts/gen-dashboards.py --check && .venv/bin/python scripts/gen-realm-imports.py --check && .venv/bin/python scripts/backlog/tracker.py --check
   scripts/dev/codeql.sh     # must end "nothing at CI's failure threshold"
   ```
   Always from the checkout's `.venv` (CONTRIBUTING.md, "Run the checks locally"); a worktree without one uses the main
   checkout's. The system `python3` lacks the dependencies, and every test then fails at import. Never start the test
   run in the background with `&`: it inherits an ignored SIGINT, and the restore tests that send Ctrl-C fail.
   Mockups: `scripts/mockups/check.py <Main.dc.html> --out <dir>` must report 0 problems.
4. **Commits** are authored as `ephico2real2` with the GitHub noreply address, and carry no tool trailers.
5. **Commit and push often.** An unpushed branch is lost work.
6. **Repo rules:** keep the public repo generic (no brand or customer names); platform credentials never pass through chat or AI tool output; diagrams, pages and screenshots are light theme only.
7. **After each merge that publishes an image:** merge the staging promotion PR, apply `main`'s staging overlay to the lab, run `check.sh`; production follows the same way from the digest proven on staging ([`docs/cicd.md`](cicd.md)).

**Practical notes.**
- Read CI failures from the check-run annotations: `gh api repos/inkcasthq/inkcast/check-runs/<id>/annotations`. A transient failure (a registry or download timeout) is re-run with `rerun-failed-jobs` once the run has finished.
- Merge with REST when GraphQL isn't available: `gh api -X PUT repos/inkcasthq/inkcast/pulls/<n>/merge -f merge_method=squash -f sha=<head sha>`.
- Before opening a PR, `git diff origin/main...HEAD --stat` must list only the story's files.
- A Dependabot image bump must land in every place the image is pinned (`tests/test_architecture.py` checks). A `github/codeql-action` bump also needs `CODEQL_BUNDLE` and its SHA-256s in `scripts/dev/codeql.sh`: the bundle is `bundleVersion` in the action's `src/defaults.json` at the pinned commit.

## Owner's steps

1. **F1.8.S5 (#38):** delete the code-scanning configuration `.github/workflows/publish.yml:codeql`, as [`docs/cicd.md`](cicd.md) ("Retiring a code-scanning configuration") describes. It can't be undone: that configuration's alerts close.
2. **F1.8.S6 (#45):** decide whether the daily `repo-settings` workflow gets a read credential with admin access to the repository settings (a GitHub App or a fine-grained token). Without one it verifies the ruleset's rules and lists the rest as NOT VERIFIED; the owner's `scripts/repo/settings.py check` covers everything.
3. **openfga/openfga#2448:** a comment proposing that goose's `Provider` lock on the connection that migrates (option 4 of the #494 design). Posting outside the repository waits for the owner's yes.
4. **The mobile app:** no epic or story covers it; `docs/product-spec.md` puts it in phase 3, ADR-0006 gives it the same Keycloak identity, and `docs/issues/13-mobile-app.md` is an unfiled draft. Whether to make it an epic and give it a wave is the owner's call.
5. **Attribution clean-up** (the owner's call):
   - strip the tool-appended "Generated by" line from old PR and issue bodies;
   - decide whether to rewrite the early history of `main` (PR #4 and earlier) to drop tool trailers and re-author early commits. This needs a force-push and is destructive.

## The tokensave fork (as of 2026-10-27)

Not revisited since 2026-10-27; check before relying on it.

- **[`ephico2real2/tokensave2`](https://github.com/ephico2real2/tokensave2)** (#227): `master` at `9f03ae6`, CI green on Linux, Windows and macOS 15, with S1 (per-turn detail), S3a and S3b (the dashboard server and page), S4 (Codex CLI sessions), S5 (`tokensave cursor sync`), the schema versions (#248), the backfill (#249) and unpriced turns (#252). Open: S2 (workspaces), S3c (`--at-login`), S6 (upstream), and #265 (the mockup pixel comparison in CI).
- **Found, not yet filed:** 2,826 Claude turns recorded before #250 still hold their message's first-line `output_tokens` (4,378,206 output tokens, $140.70 missing); a repair through `merge_line` over every transcript.
- **On the owner's Mac:** the hooks and the MCP server run `~/.cargo/bin/tokensave`; `tokensave` on `PATH` is Homebrew 7.16.0, left as it is. To bring the fork binary to `master`: `git -C ~/gitRepos/tokensave checkout master && git -C ~/gitRepos/tokensave pull --ff-only && cargo install --locked --path ~/gitRepos/tokensave`.
- **Gates** (toolchain 1.98.1): `cargo fmt --check`, `cargo clippy --workspace --all-targets --locked`, `cargo test --workspace --locked --features test-transport --no-fail-fast`. Tests and probes never write the real `~/.tokensave` or `~/.claude`: use a scratch `TOKENSAVE_HOME` with `TOKENSAVE_SKIP_AGENT_MAINTENANCE=1`.

## Resume prompt

Paste this into a new session, in the repo:

> Read `docs/handover.md`, `CONTRIBUTING.md`, `docs/contributor-map.md` and `docs/definition-of-done.md`. Check `main` is green, list open PRs, and run `scripts/backlog/tracker.py --drift inkcasthq/inkcast`. Then continue with the first open wave in `docs/backlog/order.yaml`, one issue per PR, following "How the work is done". Report each finished wave on #360 and update `docs/handover.md` at the end of the session.

---
<!-- inkcast-nav -->
[Docs](README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)
